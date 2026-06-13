from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterable

import numpy as np

from app.models import ConvertMethod, FasSensorBinding, GcsSensorBinding, SensorEntry, SourceTarget, SystemConfig


def linear_interpolate(raw_value: float, points: list[tuple[float, float]] | None, degree: int = 1) -> float:
    if not points or len(points) <= degree:
        return raw_value

    calibration_points = np.asarray(points, dtype=float)
    if calibration_points.ndim != 2 or calibration_points.shape[1] != 2:
        raise ValueError("Calibration must be a list of [voltage, reading] pairs.")

    voltages, readings = calibration_points[:, 0], calibration_points[:, 1]
    m, b = np.polyfit(voltages, readings, degree)
    return m * raw_value + b


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
    def _gcs_lookup(sensors: Iterable[SensorEntry]) -> dict[tuple[int, int], SensorEntry]:
        return {(s.binding.hat_id, s.binding.channel_id): s for s in sensors
                if isinstance(s.binding, GcsSensorBinding)}

    @staticmethod
    def _fas_lookup(sensors: Iterable[SensorEntry]) -> dict[tuple[str, int], SensorEntry]:
        return {(s.binding.node, s.binding.channel): s for s in sensors
                if isinstance(s.binding, FasSensorBinding)}

    def parse(self, source: str, raw_sensors: list[dict], config: SystemConfig, calibration_enabled: bool) -> list[ParsedSensor]:
        target = self._resolve_source(source)
        is_fas = target == SourceTarget.FAS

        if is_fas:
            lookup_fas = self._fas_lookup(config.sensors)
        else:
            lookup_gcs = self._gcs_lookup(config.sensors)

        parsed: list[ParsedSensor] = []
        for item in raw_sensors:
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
                value = linear_interpolate(raw_value, sensor_cfg.convert.calibration)
            averaged_value = self._rolling_store.add(sensor_cfg.name, value)

            parsed.append(
                ParsedSensor(
                    name=sensor_cfg.name,
                    value=round(value, 2),
                    avg=round(averaged_value, 2),
                    unit=sensor_cfg.unit,
                    timestamp=timestamp,
                )
            )

        return parsed
