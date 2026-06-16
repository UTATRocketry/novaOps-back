from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.models import ActuatorEntry, ActuatorType

if TYPE_CHECKING:
    from app.models import SystemConfig


@dataclass
class RuntimeState:
    calibration_enabled: bool = True
    data_saving_enabled: bool = False
    latest_sensors: list[dict] = field(default_factory=list)
    latest_engine_data: list[dict] = field(default_factory=list)
    latest_flight_data: dict[str, Any] = field(default_factory=dict)
    latest_events: list[Any] = field(default_factory=list)
    # Fail-safe default: until novaLock is running and affirmatively reports
    # "unlocked", we treat the physical lockout as engaged so hazardous
    # commands stay blocked (e.g. when novaLock isn't running at all).
    lockout_state: str = "locked"
    actuator_states: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def lockout_is_locked(self) -> bool:
        # Anything other than an explicit "unlocked" (including missing/unknown
        # states from a novaLock that never reported in) counts as locked.
        return self.lockout_state.strip().lower() != "unlocked"

    def update_actuator_state(self, actuator: ActuatorEntry, state: str) -> None:
        entry = self.actuator_states.get(actuator.name)
        if not isinstance(entry, dict):
            entry = {}

        lower = state.strip().lower()
        if actuator.type == ActuatorType.SERVO:
            if lower in {"enable", "enabled", "disable", "disabled"}:
                entry["enable"] = "enabled" if lower in {"enable", "enabled"} else "disabled"
            elif lower in {"on", "off"}:
                entry["power"] = lower
            else:
                entry["position"] = state
                entry["enable"] = "enabled"
        elif actuator.type == ActuatorType.SOLENOID:
            entry["position"] = lower if lower in {"open", "closed"} else state
        elif actuator.type == ActuatorType.POWERED_GPIO_DEVICE:
            if lower in {"on", "off"}:
                entry["power"] = lower
            if lower in {"armed", "arm"}:
                entry["arming"] = "armed"
            if lower in {"disarmed", "disarm"}:
                entry["arming"] = "disarmed"
        elif actuator.type == ActuatorType.GPIO_DEVICE:
            if lower in {"armed", "arm"}:
                entry["arming"] = "armed"
            if lower in {"disarmed", "disarm"}:
                entry["arming"] = "disarmed"
        else:
            if lower in {"on", "off"}:
                entry["power"] = lower
            if lower in {"armed", "disarmed"}:
                entry["arming"] = lower

        self.actuator_states[actuator.name] = entry

    def initialize_all(self, config: SystemConfig) -> None:
        for actuator in config.all_actuators():
            init_state: dict[str, str] = {}
            if actuator.type == ActuatorType.SERVO:
                if actuator.actions.default_position is not None:
                    init_state["position"] = actuator.actions.default_position
                init_state["enable"] = "disabled"
                init_state["power"] = "off"
            elif actuator.type == ActuatorType.SOLENOID:
                init_state["position"] = "closed"
            elif actuator.type == ActuatorType.GPIO_DEVICE:
                init_state["arming"] = "disarmed"
            else:
                init_state["power"] = "off"
                init_state["arming"] = "disarmed"
            self.actuator_states[actuator.name] = init_state
