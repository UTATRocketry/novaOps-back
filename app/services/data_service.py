from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Iterable

import numpy as np

from app.models import (
    ConvertMethod,
    ConvertSpec,
    FasSensorBinding,
    GcsSensorBinding,
    PiGpioSensorBinding,
    SensorEntry,
    SensorType,
    SourceTarget,
    SystemConfig,
)

LOGGER = logging.getLogger(__name__)


def linear_interpolate(raw_value: float, points: list[tuple[float, float]] | None, degree: int = 1) -> float:
    if not points or len(points) <= degree:
        return raw_value

    calibration_points = np.asarray(points, dtype=float)
    if calibration_points.ndim != 2 or calibration_points.shape[1] != 2:
        raise ValueError("Calibration must be a list of [voltage, reading] pairs.")

    voltages, readings = calibration_points[:, 0], calibration_points[:, 1]
    m, b = np.polyfit(voltages, readings, degree)
    return m * raw_value + b


def _pairwise_slopes(xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """slopes[i, j] is the slope from point i to point j; NaN where i == j or the x values match."""
    dx = xs[None, :] - xs[:, None]
    dy = ys[None, :] - ys[:, None]
    with np.errstate(divide="ignore", invalid="ignore"):
        slopes = dy / dx
    slopes[dx == 0] = np.nan
    return slopes


def theil_sen_fit(xs: np.ndarray, ys: np.ndarray) -> tuple[float, float] | None:
    """Slope is the median of the slopes between every pair of points. Ignores up to ~29% bad points."""
    slopes = _pairwise_slopes(xs, ys)[np.triu_indices(len(xs), k=1)]
    slopes = slopes[~np.isnan(slopes)]
    if slopes.size == 0:
        return None
    m = float(np.median(slopes))
    return m, float(np.median(ys - m * xs))


def repeated_median_fit(xs: np.ndarray, ys: np.ndarray) -> tuple[float, float] | None:
    """Siegel's repeated median: each point's median slope to the others, then the median of those.

    Ignores up to half the points being bad.
    """
    slopes = _pairwise_slopes(xs, ys)
    has_slope = ~np.all(np.isnan(slopes), axis=1)
    if not has_slope.any():
        return None
    m = float(np.median(np.nanmedian(slopes[has_slope], axis=1)))
    return m, float(np.median(ys - m * xs))


_ROBUST_FITS = {
    ConvertMethod.THEIL_SEN: theil_sen_fit,
    ConvertMethod.REPEATED_MEDIAN: repeated_median_fit,
}


@lru_cache(maxsize=512)
def _robust_line(method: ConvertMethod, points: tuple[tuple[float, float], ...]) -> tuple[float, float] | None:
    # Cached so a sensor's table is fitted once, not on every sample.
    calibration_points = np.asarray(points, dtype=float)
    if calibration_points.ndim != 2 or calibration_points.shape[1] != 2:
        raise ValueError("Calibration must be a list of [voltage, reading] pairs.")
    return _ROBUST_FITS[method](calibration_points[:, 0], calibration_points[:, 1])


def robust_interpolate(raw_value: float, points: list[tuple[float, float]] | None, method: ConvertMethod) -> float:
    """Apply a robust straight-line fit. Like `linear_interpolate`, the line carries on past the
    outermost points, and fewer than two points with different raw values pass the raw value through."""
    if not points or len(points) < 2:
        return raw_value
    line = _robust_line(method, tuple((float(x), float(y)) for x, y in points))
    if line is None:
        return raw_value
    m, b = line
    return m * raw_value + b


def convert_value(raw_value: float, convert: ConvertSpec) -> float:
    """Calibrate one raw reading with the sensor's conversion method (not called for `none`)."""
    if convert.method in _ROBUST_FITS:
        return robust_interpolate(raw_value, convert.calibration, convert.method)
    # linear, and zero_offset / polynomial, which aren't implemented separately yet.
    return linear_interpolate(raw_value, convert.calibration)


@dataclass
class ParsedSensor:
    name: str
    value: float | None
    avg: float | None
    unit: str
    timestamp: int
    # Set (with value and avg None) when a thermocouple reports a fault code.
    fault: str | None = None

    def to_dict(self) -> dict:
        """The WebSocket form. `fault` is only present on a faulted reading."""
        entry = dict(self.__dict__)
        if entry["fault"] is None:
            del entry["fault"]
        return entry


# What the daqhats library returns, with a successful return code, for a
# thermocouple it can't read (mcc134.h: OPEN_TC_VALUE, COMMON_MODE_TC_VALUE,
# OVERRANGE_TC_VALUE). Matched exactly: they are sentinels, not temperatures.
TC_FAULT_VALUES = {
    -9999.0: "open",
    -7777.0: "common_mode",
    -8888.0: "overrange",
}

# Two sources feeding one target within this window is worth a warning (e.g.
# novaMock running on a broker where the real novaGround is also up).
DUPLICATE_SOURCE_WINDOW_S = 5.0


class RollingAverageStore:
    def __init__(self, window_size: int = 100) -> None:
        self._window_size = window_size
        self._values: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=self._window_size))

    def add(self, key: str, value: float) -> float:
        bucket = self._values[key]
        bucket.append(value)
        return np.mean(np.array(bucket))


# Telemetry "source" strings mapped to config SourceTarget.
# Legacy novaGround/novaThermo/novaFAS names kept so existing publishers keep working.
_SOURCE_ALIASES = {
    "gcs": SourceTarget.GCS,
    "novaground": SourceTarget.GCS,
    "novaops": SourceTarget.GCS,
    # novaMock is a stand-in for novaGround (docs/design/pi-io-config.md).
    "novamock": SourceTarget.GCS,
    "tcs": SourceTarget.TCS,
    "novathermo": SourceTarget.TCS,
    "fas": SourceTarget.FAS,
    "novafas": SourceTarget.FAS,
}


class SensorParser:
    def __init__(self, rolling_store: RollingAverageStore,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._rolling_store = rolling_store
        self._clock = clock
        # Unknown source names already logged, so each is logged once.
        self._unknown_sources: set[str] = set()
        # target -> {source name: when it last sent telemetry}
        self._target_sources: dict[SourceTarget, dict[str, float]] = defaultdict(dict)
        # (target, sources) -> when the two-sources warning was last logged
        self._duplicate_warned: dict[tuple, float] = {}

    @staticmethod
    def _resolve_source(source: str) -> SourceTarget | None:
        return _SOURCE_ALIASES.get(source.strip().lower())

    def _note_source(self, source: str, target: SourceTarget) -> None:
        """Warn when one target gets telemetry from two sources at once."""
        now = self._clock()
        seen = self._target_sources[target]
        seen[source] = now
        others = sorted(name for name, last in seen.items()
                        if name != source and now - last <= DUPLICATE_SOURCE_WINDOW_S)
        if not others:
            return
        key = (target, frozenset([source, *others]))
        last_warned = self._duplicate_warned.get(key)
        if last_warned is not None and now - last_warned < 60.0:
            return
        self._duplicate_warned[key] = now
        LOGGER.warning(
            "%s telemetry is arriving from more than one source within %.0fs: %s. "
            "Both act on %s commands; is novaMock running next to the real node?",
            target.value, DUPLICATE_SOURCE_WINDOW_S, ", ".join(sorted([source, *others])), target.value,
        )

    @staticmethod
    def _gpio_lookup(sensors: Iterable[SensorEntry], target: SourceTarget) -> dict[int, SensorEntry]:
        return {s.binding.gpio_channel: s for s in sensors
                if isinstance(s.binding, PiGpioSensorBinding) and s.binding.source == target}

    def _parse_gpios(self, gpios: list, config: SystemConfig, target: SourceTarget,
                     default_timestamp: int) -> list[ParsedSensor]:
        """Turn a Pi's `gpios` telemetry ([{pin_id, state}]) into DI readings of 0 or 1."""
        lookup = self._gpio_lookup(config.sensors, target)
        if not lookup:
            return []
        parsed: list[ParsedSensor] = []
        for item in gpios:
            if not isinstance(item, dict):
                continue
            try:
                pin = int(item.get("pin_id"))
                value = 1 if int(item.get("state")) else 0
                timestamp = int(item.get("timestamp", default_timestamp))
            except (TypeError, ValueError):
                continue
            sensor_cfg = lookup.get(pin)
            if sensor_cfg is None:
                continue
            parsed.append(ParsedSensor(name=sensor_cfg.name, value=value, avg=value,
                                       unit=sensor_cfg.unit, timestamp=timestamp))
        return parsed

    @staticmethod
    def _gcs_lookup(sensors: Iterable[SensorEntry], target: SourceTarget) -> dict[tuple[int, int], SensorEntry]:
        return {(s.binding.hat_id, s.binding.channel_id): s for s in sensors
                if isinstance(s.binding, GcsSensorBinding) and s.binding.source == target}

    @staticmethod
    def _fas_lookup(sensors: Iterable[SensorEntry]) -> dict[tuple[str, int], SensorEntry]:
        return {(s.binding.resolved_node, s.binding.channel): s for s in sensors
                if isinstance(s.binding, FasSensorBinding)}

    def parse(self, source: str, raw_sensors: list[dict] | dict[str, dict], config: SystemConfig,
              calibration_enabled: bool, gpios: list | None = None) -> list[ParsedSensor]:
        target = self._resolve_source(source)
        if target is None:
            # An unknown source used to be read as GCS. Drop it instead, so a
            # stray publisher can't write into GCS sensors.
            name = source.strip()
            if name.lower() not in self._unknown_sources:
                self._unknown_sources.add(name.lower())
                LOGGER.warning("Dropping telemetry from unknown source %r", name)
            return []
        self._note_source(source.strip().lower(), target)
        is_fas = target == SourceTarget.FAS

        if is_fas:
            lookup_fas = self._fas_lookup(config.sensors)
        else:
            lookup_gcs = self._gcs_lookup(config.sensors, target)

        # Accept either a list of sensor dicts or a dict keyed by "node:channel"
        items = raw_sensors.values() if isinstance(raw_sensors, dict) else raw_sensors

        parsed: list[ParsedSensor] = []
        newest_timestamp = 0
        for item in items:
            try:
                timestamp = int(item.get("timestamp", 0))
                raw_value = float(item.get("value", 0.0))
            except (TypeError, ValueError):
                # One malformed reading (e.g. a null value) must not drop the
                # rest of the packet.
                continue
            newest_timestamp = max(newest_timestamp, timestamp)

            if is_fas:
                key = (str(item.get("node", "")), int(item.get("channel", -1)))
                sensor_cfg = lookup_fas.get(key)
            else:
                key = (int(item.get("hat_id", -1)), int(item.get("channel_id", -1)))
                sensor_cfg = lookup_gcs.get(key)

            if sensor_cfg is None:
                continue

            fault = TC_FAULT_VALUES.get(raw_value) if (
                not is_fas and sensor_cfg.type == SensorType.TC) else None
            if fault is not None:
                # No value, and kept out of the rolling average.
                parsed.append(ParsedSensor(name=sensor_cfg.name, value=None, avg=None,
                                           unit=sensor_cfg.unit, timestamp=timestamp, fault=fault))
                continue

            value = raw_value
            if calibration_enabled and sensor_cfg.convert.method != ConvertMethod.NONE:
                value = convert_value(raw_value, sensor_cfg.convert)
            averaged_value = self._rolling_store.add(sensor_cfg.name, value)

            parsed.append(
                ParsedSensor(
                    name=sensor_cfg.name,
                    value=round(value, 2),
                    avg=round(averaged_value, 2),
                    unit= "V" if sensor_cfg.convert.method != ConvertMethod.NONE and not calibration_enabled else sensor_cfg.unit,
                    timestamp=timestamp,
                )
            )

        if gpios and not is_fas:
            default_timestamp = newest_timestamp or int(time.time() * 1000)
            parsed.extend(self._parse_gpios(gpios, config, target, default_timestamp))

        return parsed
