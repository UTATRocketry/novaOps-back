from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterable
import numpy as np
from app.models import ActuatorConfig, ActuatorType, CommandPayload, SensorConfig, SystemConfig


def linear_interpolate(raw_value: float, points: list[tuple[float, float]], degree: int = 1) -> float:
    calibration_points = np.array(points)
    voltages, readings = calibration_points[:, 0], calibration_points[:, 1]
    m, b = np.polyfit(voltages, readings, degree)
    return m*raw_value + b


@dataclass
class ParsedSensor:
    name: str
    value: float
    avg: float
    unit: str
    timestamp: int


class RollingAverageStore:
    def __init__(self, window_size: int = 5) -> None:
        self._window_size = window_size
        self._values: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=self._window_size))

    def add(self, key: str, value: float) -> float:
        bucket = self._values[key]
        bucket.append(value)
        return sum(bucket) / len(bucket)


class SensorParser:
    def __init__(self, rolling_store: RollingAverageStore) -> None:
        self._rolling_store = rolling_store

    @staticmethod
    def _source_key(source: str) -> str:
        return source.strip().lower()

    @staticmethod
    def _sensor_lookup(sensors: Iterable[SensorConfig]) -> dict[tuple[int, int], SensorConfig]:
        return {(s.hat_id, s.channel_id): s for s in sensors}

    def parse(self, source: str, raw_sensors: list[dict], config: SystemConfig, calibration_enabled: bool) -> list[ParsedSensor]:
        source_key = self._source_key(source)

        if source_key == "novaground":
            sensors_cfg = config.mcc128daq or config.mccdaq
        elif source_key == "novathermo":
            sensors_cfg = config.mcc134daq
        elif source_key == "novafas":
            sensors_cfg = config.fas
        else:
            sensors_cfg = config.all_sensors()

        lookup = self._sensor_lookup(sensors_cfg)

        parsed: list[ParsedSensor] = []
        for item in raw_sensors:
            hat_id = int(item.get("hat_id", -1))
            channel_id = int(item.get("channel_id", -1))
            timestamp = int(item.get("timestamp", 0))
            raw_value = float(item.get("value", 0.0))

            sensor_cfg = lookup.get((hat_id, channel_id))
            if sensor_cfg is None:
                continue

            value = raw_value
            if calibration_enabled:
                value = linear_interpolate(raw_value, sensor_cfg.calibration)
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


class CommandParser:
    RELAY_TYPES = {ActuatorType.SOLENOID, ActuatorType.POWERED_DEVICE, ActuatorType.POWERED_GPIO_DEVICE}

    def __init__(self, config: SystemConfig) -> None:
        self._config = config

    @staticmethod
    def _is_on_state(state: str) -> bool:
        normalized = state.strip().lower()
        return normalized in {"on", "open", "1", "true", "fill", "armed", "position_1", "position_2", "position_3"}

    @staticmethod
    def _resolve_relay_state(state: str, relay_type: str | None, solenoid_type: str | None) -> int:
        state_lower = state.strip().lower()
        relay_type_value = (relay_type or "NO").upper()
        solenoid_type_value = (solenoid_type or "NO").upper()

        if state_lower in {"open", "closed"}:
            power_on = (
                (state_lower == "open" and solenoid_type_value == "NC")
                or (state_lower == "closed" and solenoid_type_value == "NO")
            )
        else:
            power_on = CommandParser._is_on_state(state_lower)

        return 0 if ((power_on and relay_type_value == "NO") or ((not power_on) and relay_type_value == "NC")) else 1

    def _find_actuator(self, name: str) -> ActuatorConfig:
        for actuator in self._config.all_actuators():
            if actuator.name == name:
                return actuator
        raise ValueError(f"Actuator '{name}' not found in config")

    def parse(self, payload: CommandPayload) -> list[dict]:
        actuator = self._find_actuator(payload.name)
        state = payload.state.strip()
        state_lower = state.lower()
        
        if actuator.name == "BVOTP": # temporary fix
            actuator.relay_id = 0

        if actuator.actuator_type in self.RELAY_TYPES:
            relay_state = self._resolve_relay_state(state, actuator.relay_type, actuator.solenoid_type)
            command_type = "gpio" if actuator.actuator_type == ActuatorType.POWERED_GPIO_DEVICE else "relay"
            return [{"type": command_type, "id": actuator.channel_id, "state": relay_state}]
        if actuator.actuator_type in {ActuatorType.SERVO, ActuatorType.SERVO3}:
            if state_lower in {"enable", "disable"}:
                angle_state = "on" if state_lower == "enable" else "off"
                return [{"type": "servo", "id": actuator.channel_id, "angle": angle_state}]
            if state_lower in {"on", "off"}:
                if actuator.relay_id is None:
                    raise ValueError(
                        f"Servo '{payload.name}' does not define relayID, so state '{payload.state}' is invalid"
                    )
                relay_state = self._resolve_relay_state(state_lower, actuator.relay_type, None)
                return [{"type": "relay", "id": int(actuator.relay_id), "state": relay_state}]

            alias_lookup = {alias.lower(): pos for alias, pos in zip(actuator.position_aliases, actuator.positions)}
            alias_lookup.update(
                {
                    "position_1": actuator.positions[0] if len(actuator.positions) > 0 else None,
                    "position_2": actuator.positions[1] if len(actuator.positions) > 1 else None,
                    "position_3": actuator.positions[2] if len(actuator.positions) > 2 else None,
                    "open": actuator.positions[0] if actuator.positions else None,
                    "closed": actuator.positions[-1] if actuator.positions else None,
                }
            )

            angle = alias_lookup.get(state_lower)
            if angle is None:
                raise ValueError(f"Unsupported servo state '{payload.state}' for actuator '{payload.name}'")
            return [{"type": "servo", "id": actuator.channel_id, "angle": int(angle)}]

        raise ValueError(f"Unsupported actuator type: {actuator.actuator_type}")

