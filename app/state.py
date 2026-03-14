from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RuntimeState:
    calibration_enabled: bool = True
    data_saving_enabled: bool = False
    latest_sensors: list[dict] = field(default_factory=list)
    actuator_states: dict[str, dict[str, str]] = field(default_factory=dict)
