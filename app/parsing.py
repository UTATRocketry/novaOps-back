from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterable
import numpy as np
from app.models import (
    ActuatorEntry,
    ActuatorActions,
    ActuatorType,
    CommandPayload,
    ConvertMethod,
    SensorEntry,
    SourceTarget,
    SystemConfig,
)


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


# Telemetry "source" strings (incoming packets) mapped to the config SourceTarget.
# Legacy novaGround/novaThermo/novaFAS names are kept so existing publishers keep working.
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
        return {(s.binding.hat_id, s.binding.channel_id): s for s in sensors}

    @staticmethod
    def _fas_lookup(sensors: Iterable[SensorEntry]) -> dict[tuple[str, int], SensorEntry]:
        return {(s.binding.node, s.binding.channel): s for s in sensors}

    def parse(self, source: str, raw_sensors: list[dict], config: SystemConfig, calibration_enabled: bool) -> list[ParsedSensor]:
        target = self._resolve_source(source)
        sensors_cfg = [s for s in config.sensors if s.binding.source == target] if target else config.sensors

        is_fas = target == SourceTarget.FAS
        if is_fas:
            lookup_fas = self._fas_lookup(sensors_cfg)
        else:
            lookup_gcs = self._gcs_lookup(sensors_cfg)

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


class CommandParser:
    RELAY_TYPES = {ActuatorType.SOLENOID, ActuatorType.POWERED_DEVICE, ActuatorType.POWERED_GPIO_DEVICE}

    def __init__(self, config: SystemConfig) -> None:
        self._config = config

    @staticmethod
    def _is_on_state(state: str) -> bool:
        normalized = state.strip().lower()
        return normalized in {"on", "open", "1", "true", "fill", "armed", "arm", "position_1", "position_2", "position_3"}

    @staticmethod
    def _resolve_power(state: str, actions: ActuatorActions) -> bool:
        """Return True when the underlying relay/device should be energized."""
        state_lower = state.strip().lower()
        solenoid_type = (actions.solenoid_type or "nominally_open").lower()
        if state_lower in {"open", "closed"}:
            return (
                (state_lower == "open" and solenoid_type == "nominally_closed")
                or (state_lower == "closed" and solenoid_type == "nominally_open")
            )
        return CommandParser._is_on_state(state_lower)

    @staticmethod
    def _resolve_relay_state(state: str, relay_type: str | None, solenoid_type: str | None) -> int:
        state_lower = state.strip().lower()
        relay = (relay_type or "nominally_off").lower()
        solenoid = (solenoid_type or "nominally_open").lower()

        if state_lower in {"open", "closed"}:
            power_on = (
                (state_lower == "open" and solenoid == "nominally_closed")
                or (state_lower == "closed" and solenoid == "nominally_open")
            )
        else:
            power_on = CommandParser._is_on_state(state_lower)

        return 0 if ((power_on and relay == "nominally_off") or ((not power_on) and relay == "nominally_on")) else 1

    def _find_actuator(self, name: str) -> ActuatorEntry:
        actuator = self._config.find_actuator(name)
        if actuator is None:
            raise ValueError(f"Actuator '{name}' not found in config")
        return actuator

    def parse(self, payload: CommandPayload) -> list[dict]:
        actuator = self._find_actuator(payload.name)
        if actuator.binding.target == SourceTarget.GCS:
            return self._parse_gcs(actuator, payload.state)
        return self._parse_fas(actuator, payload.state)

    def _parse_gcs(self, actuator: ActuatorEntry, state: str) -> list[dict]:
        binding = actuator.binding
        actions = actuator.actions
        state = state.strip()
        state_lower = state.lower()

        if actuator.type in self.RELAY_TYPES:
            relay_state = self._resolve_relay_state(state, actions.relay_type, actions.solenoid_type)
            command_type = "gpio" if actuator.type == ActuatorType.POWERED_GPIO_DEVICE else "relay"
            return [{"type": command_type, "id": binding.relay_channel, "state": relay_state}]

        if actuator.type == ActuatorType.SERVO:
            if state_lower in {"enable", "disable"}:
                angle_state = "on" if state_lower == "enable" else "off"
                return [{"type": "servo", "id": binding.servo_channel, "angle": angle_state}]
            if state_lower in {"on", "off"}:
                if binding.relay_channel is None:
                    raise ValueError(
                        f"Servo '{actuator.name}' does not define relay_channel, so state '{state}' is invalid"
                    )
                relay_state = self._resolve_relay_state(state_lower, actions.relay_type, None)
                return [{"type": "relay", "id": int(binding.relay_channel), "state": relay_state}]

            alias_lookup = {alias.lower(): pos for alias, pos in zip(actions.position_aliases, actions.positions)}
            alias_lookup.update(
                {
                    "position_1": actions.positions[0] if len(actions.positions) > 0 else None,
                    "position_2": actions.positions[1] if len(actions.positions) > 1 else None,
                    "position_3": actions.positions[2] if len(actions.positions) > 2 else None,
                    "open": actions.positions[0] if actions.positions else None,
                    "closed": actions.positions[-1] if actions.positions else None,
                }
            )

            angle = alias_lookup.get(state_lower)
            if angle is None:
                raise ValueError(f"Unsupported servo state '{state}' for actuator '{actuator.name}'")
            return [{"type": "servo", "id": binding.servo_channel, "angle": int(angle)}]

        raise ValueError(f"Unsupported actuator type: {actuator.type}")

    def _parse_fas(self, actuator: ActuatorEntry, state: str) -> list[dict]:
        binding = actuator.binding
        actions = actuator.actions
        state = state.strip()

        if actuator.type == ActuatorType.SERVO:
            alias_lookup = {alias.lower(): pos for alias, pos in zip(actions.position_aliases, actions.positions)}
            micros = alias_lookup.get(state.lower())
            if micros is None:
                raise ValueError(f"Unsupported servo state '{state}' for actuator '{actuator.name}'")
            commands: list[dict] = []
            # FAS servos carry both a power relay and a PWM channel: power the relay, then move.
            if binding.relay_channel is not None:
                commands.append(
                    {"type": "fas", "node": binding.node, "port": "relay", "channel": binding.relay_channel, "action": "on"}
                )
            commands.append(
                {
                    "type": "fas",
                    "node": binding.node,
                    "port": "servo",
                    "channel": binding.servo_channel,
                    "action": state,
                    "value": int(micros),
                }
            )
            return commands

        if actuator.type in (ActuatorType.SOLENOID, ActuatorType.POWERED_DEVICE):
            power = self._resolve_power(state, actions)
            return [
                {
                    "type": "fas",
                    "node": binding.node,
                    "port": "relay",
                    "channel": binding.relay_channel,
                    "action": "on" if power else "off",
                }
            ]

        if actuator.type == ActuatorType.POWERED_GPIO_DEVICE:
            return [
                {
                    "type": "fas",
                    "node": binding.node,
                    "port": "gpio",
                    "channel": binding.relay_channel,
                    "action": state,
                }
            ]

        raise ValueError(f"Unsupported actuator type: {actuator.type}")

    def parse_system_command(self, name: str, state: str | None) -> list[dict]:
        command = self._config.find_command(name)
        if command is None:
            raise ValueError(f"System command '{name}' not found in config")
        if command.states is not None:
            if state is None or state not in command.states:
                raise ValueError(
                    f"System command '{name}' requires state in {command.states}, got '{state}'"
                )

        emitted: dict[str, object] = {
            "type": "fas_cmd",
            "node": command.binding.node,
            "command": name,
        }
        if command.binding.channel is not None:
            emitted["channel"] = command.binding.channel
        if state is not None:
            emitted["state"] = state
        return [emitted]
