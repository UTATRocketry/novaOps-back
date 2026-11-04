from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

import numpy as np

from app.models import ConvertMethod, ConvertSpec, FasSensorBinding, GcsSensorBinding, SensorEntry, SourceTarget, SystemConfig


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
    value: float
    avg: float
    unit: str
    timestamp: int


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
    "tcs": SourceTarget.TCS,
    "novathermo": SourceTarget.TCS,
    "fas": SourceTarget.FAS,
    "novafas": SourceTarget.FAS,
}


class SensorParser:
    def __init__(self, rolling_store: RollingAverageStore) -> None:
        self._rolling_store = rolling_store

    @staticmethod
    def _resolve_source(source: str) -> SourceTarget | None:
        return _SOURCE_ALIASES.get(source.strip().lower())

    @staticmethod
    def _gcs_lookup(sensors: Iterable[SensorEntry], target: SourceTarget) -> dict[tuple[int, int], SensorEntry]:
        return {(s.binding.hat_id, s.binding.channel_id): s for s in sensors
                if isinstance(s.binding, GcsSensorBinding) and s.binding.source == target}

    @staticmethod
    def _fas_lookup(sensors: Iterable[SensorEntry]) -> dict[tuple[str, int], SensorEntry]:
        return {(s.binding.resolved_node, s.binding.channel): s for s in sensors
                if isinstance(s.binding, FasSensorBinding)}

    def parse(self, source: str, raw_sensors: list[dict] | dict[str, dict], config: SystemConfig, calibration_enabled: bool) -> list[ParsedSensor]:
        target = self._resolve_source(source)
        is_fas = target == SourceTarget.FAS

        if is_fas:
            lookup_fas = self._fas_lookup(config.sensors)
        else:
            lookup_gcs = self._gcs_lookup(config.sensors, target or SourceTarget.GCS)

        # Accept either a list of sensor dicts or a dict keyed by "node:channel"
        items = raw_sensors.values() if isinstance(raw_sensors, dict) else raw_sensors

        parsed: list[ParsedSensor] = []
        for item in items:
            timestamp = int(item.get("timestamp", 0))
            raw_value = float(item.get("value", 0.0))

            if is_fas:
                key = (str(item.get("node", "")), int(item.get("channel", -1)))
                sensor_cfg = lookup_fas.get(key)
            else:
                key = (int(item.get("hat_id", -1)), int(item.get("channel_id", -1)))
                sensor_cfg = lookup_gcs.get(key)

            if sensor_cfg is None:
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

        return parsed
