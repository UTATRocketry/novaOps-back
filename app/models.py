from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class SourceTarget(str, Enum):
    GCS = "GCS"
    FAS = "FAS"
    TCS = "TCS"
    OPS = "OPS"


class SensorType(str, Enum):
    PT = "PT"
    LC = "LC"
    TC = "TC"


class ActuatorType(str, Enum):
    SERVO = "servo"
    SOLENOID = "solenoid"
    POWERED_DEVICE = "powered_device"
    POWERED_GPIO_DEVICE = "powered_gpio_device"
    GPIO_DEVICE = "gpio_device"


class ConvertMethod(str, Enum):
    NONE = "none"
    ZERO_OFFSET = "zero_offset"
    LINEAR = "linear"
    POLYNOMIAL = "polynomial"


class ConvertSpec(BaseModel):
    method: ConvertMethod = ConvertMethod.LINEAR
    calibration: list[tuple[float, float]] | None = None


class GcsSensorBinding(BaseModel):
    source: Literal["GCS", "TCS"]
    hat_id: int
    channel_id: int


class FasSensorBinding(BaseModel):
    source: Literal["FAS"]
    node: str | None = None 
    board_type: str | None = None 
    board_id: int = 0  
    channel: int

    @property
    def resolved_node(self) -> str | None:
        if self.node:
            return self.node
        if self.board_type is None:
            return None
        return f"{self.board_type}_{self.board_id}"


class SensorEntry(BaseModel):
    name: str
    type: SensorType
    unit: str = ""
    range: tuple[float, float] | None = None
    binding: GcsSensorBinding | FasSensorBinding = Field(discriminator="source")
    convert: ConvertSpec = Field(default_factory=ConvertSpec)


class ActuatorBinding(BaseModel):
    target: SourceTarget
    node: str | None = None          # legacy FAS node string, e.g. "EPB_1"
    board_type: str | None = None    # FAS board kind, e.g. "EPB", "FMC"
    board_id: int = 0                # 0-based FAS board index (default 0)
    relay_channel: int | None = None
    servo_channel: int | None = None
    gpio_channel: int | None = None


class ActuatorActions(BaseModel):
    # servo
    position_aliases: list[str] = Field(default_factory=list)
    positions: list[int] = Field(default_factory=list)
    default_position: str | int | None = Field(
        default=None,
        validation_alias=AliasChoices("defaultPosition", "default_position"),
    )
    # relay / solenoid / powered
    relay_type: str | None = None  # "nominally_off" / "nominally_on"
    solenoid_type: str | None = None  # "nominally_closed" / "nominally_open"
    gpio_commands: list[str] = Field(default_factory=list)  # e.g. [ARM, DISARM]

    @model_validator(mode="after")
    def _servo_arrays_match(self) -> "ActuatorActions":
        if self.position_aliases and len(self.position_aliases) != len(self.positions):
            raise ValueError("position_aliases and positions length mismatch")
        return self


class ActuatorEntry(BaseModel):
    name: str
    type: ActuatorType
    binding: ActuatorBinding
    actions: ActuatorActions = Field(default_factory=ActuatorActions)

    @model_validator(mode="after")
    def _fas_needs_board(self) -> "ActuatorEntry":
        if self.binding.target == SourceTarget.FAS:
            if self.binding.node is None and self.binding.board_type is None:
                raise ValueError(
                    f"FAS actuator '{self.name}' must set either binding.node (legacy) "
                    "or binding.board_type"
                )
        return self


class CommandBinding(BaseModel):
    target: SourceTarget
    node: str | None = None
    channel: int | None = None


class CommandEntry(BaseModel):
    binding: CommandBinding
    states: list[str] | None = None  # e.g. [STANDBY, ARMED]


class SafetyRules(BaseModel):
    critical: list[dict[str, str | list[str]]] = Field(default_factory=list)
    hazardous: list[dict[str, str | list[str]]] = Field(default_factory=list)


class DeviceEntry(BaseModel):
    key: str                                           # e.g. "EPB:0", "FMC:0"
    label: str = ""
    ranges: dict[str, tuple[float, float]] = Field(default_factory=dict)


class PacketFieldSpec(BaseModel):
    key: str
    type: str        # "number" | "bool" | "string"
    default: Any = None


class PacketEntry(BaseModel):
    name: str
    op: str
    fields: list[PacketFieldSpec] = Field(default_factory=list)


class Procedure(BaseModel):
    name: str
    steps: list[str] = Field(default_factory=list)


class SystemConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    sensors: list[SensorEntry] = Field(default_factory=list, alias="Sensors")
    actuators: list[ActuatorEntry] = Field(default_factory=list, alias="Actuators")
    commands: dict[str, CommandEntry] = Field(default_factory=dict, alias="Commands")
    safety_rules: SafetyRules = Field(default_factory=SafetyRules, alias="safetyRules")
    procedures: list[Procedure] = Field(default_factory=list, alias="Procedures")
    devices: list[DeviceEntry] = Field(default_factory=list, alias="Devices")
    packets: list[PacketEntry] = Field(default_factory=list, alias="Packets")

    @model_validator(mode="before")
    @classmethod
    def normalize_commands(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        commands = data.get("Commands")
        if not isinstance(commands, list):
            return data

        normalized_commands: dict[str, Any] = {}
        for command in commands:
            if not isinstance(command, dict):
                continue

            name = command.get("name")
            if isinstance(name, str) and name:
                normalized_commands[name] = {key: value for key, value in command.items() if key != "name"}

        return {**data, "Commands": normalized_commands}

    def find_sensor(self, name: str) -> SensorEntry | None:
        for sensor in self.sensors:
            if sensor.name == name:
                return sensor
        return None

    def find_actuator(self, name: str) -> ActuatorEntry | None:
        for actuator in self.actuators:
            if actuator.name == name:
                return actuator
        return None

    def find_command(self, name: str) -> CommandEntry | None:
        return self.commands.get(name)

    def all_sensors(self) -> list[SensorEntry]:
        return list(self.sensors)

    def all_actuators(self) -> list[ActuatorEntry]:
        return list(self.actuators)

    def all_procedures(self) -> list[Procedure]:
        return list(self.procedures)

    def find_procedure(self, name: str) -> Procedure | None:
        for procedure in self.procedures:
            if procedure.name == name:
                return procedure
        return None

    def is_hazardous_command(self, name: str, state: str | None) -> bool:
        return self._matches_safety_rule(self.safety_rules.hazardous, name, state)
    
    def is_critical_command(self, name: str, state: str | None) -> bool:
        return self._matches_safety_rule(self.safety_rules.critical, name, state)

    @staticmethod
    def _matches_safety_rule(rules: list[dict[str, str | list[str]]], name: str, state: str | None) -> bool:
        requested_name = name.strip().upper()
        requested_state = (state or "").strip().upper()

        for rule in rules:
            for rule_name, rule_states in rule.items():
                if rule_name.strip().upper() != requested_name:
                    continue

                if isinstance(rule_states, str):
                    states = [rule_states]
                else:
                    states = rule_states

                normalized_states = [item.strip().upper() for item in states]
                return "ALL" in normalized_states or requested_state in normalized_states

        return False


class FlagPayload(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {"enabled": True}
        }
    )

    enabled: bool = Field(description="Boolean flag value")


class CommandPayload(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "type": "solenoid",
                "name": "SVOTV",
                "state": "open"
            }
        }
    )

    type: str = Field(description="Frontend actuator type label")
    name: str = Field(description="Actuator name from config")
    state: str = Field(description="Requested state (e.g., open/closed/on/off/alias)")


class SystemCommandPayload(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "SET_FLIGHT_STATE",
                "state": "ARMED"
            }
        }
    )

    name: str = Field(description="System command name from the Commands config section")
    state: str | None = Field(default=None, description="Optional command state argument")


class DirectRelayPayload(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"target": "GCS", "channel": 8, "state": 1}}
    )

    target: Literal["GCS", "FAS"]
    node: str | None = Field(
        default=None,
        description='FAS only — board node string, e.g. "EPB_4". Parsed to board_type/board_id.',
    )
    channel: int = Field(ge=0, description="Relay channel number")
    state: Literal[0, 1] = Field(description="0 = off, 1 = on")


class DirectServoPayload(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"target": "FAS", "node": "EPB_4", "channel": 1, "pulse_us": 1500}}
    )

    target: Literal["GCS", "FAS"]
    node: str | None = Field(
        default=None,
        description='FAS only — board node string, e.g. "EPB_4".',
    )
    channel: int = Field(ge=0, description="Servo/PWM channel number")
    pulse_us: int = Field(ge=0, le=3000, description="PWM pulse width in microseconds (0 = disable)")


class IncomingSensorPacket(BaseModel):
    source: str
    sensors: list[dict[str, Any]]


class RoleAssignPayload(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "role": "operator",
                "password": None,
                "target_client_id": None,
            }
        }
    )

    role: str = Field(description="Target role: viewer, pad, operator, or admin")
    password: str | None = Field(default=None, description="Required when requesting the admin role")
    target_client_id: str | None = Field(
        default=None,
        description="Target client UUID. Omit to change your own role. Only admin callers may set this.",
    )
