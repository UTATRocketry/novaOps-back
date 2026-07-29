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
    buzzer_melodies: dict[str, list[list[int]]] = Field(
        default_factory=dict,
        alias="BuzzerMelodies",
        description=(
            "Named FMC buzzer melodies. Each is a list of [freq_hz, dur_ms] or "
            "[freq_hz, dur_ms, vol] notes; freq_hz 0 is a rest (silence)."
        ),
    )

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

    def find_buzzer_melody(self, name: str) -> list[list[int]] | None:
        return self.buzzer_melodies.get(name)

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


class FasBuzzerPayload(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"node": "FMC_0", "action": "play", "melody": "test_chime"}}
    )

    node: str | None = Field(
        default=None,
        description='FAS board node string, e.g. "FMC_0". Parsed to board_type/board_id.',
    )
    action: Literal["play", "stop"] = Field(
        default="play",
        description="play = stream a melody to the FMC and play it; stop = silence now",
    )
    melody: str | None = Field(
        default=None,
        description="Name of a predefined melody from config (BuzzerMelodies). Used when notes is omitted.",
    )
    notes: list[list[int]] | None = Field(
        default=None,
        description=(
            "Explicit melody: list of [freq_hz, dur_ms] or [freq_hz, dur_ms, vol] "
            "notes; freq_hz 0 is a rest. Takes precedence over melody."
        ),
    )


class FasSdPayload(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"node": "FMC_0", "action": "set_rate", "divisor": 10}}
    )

    node: str | None = Field(
        default=None,
        description='FAS board node string, e.g. "FMC_0". Parsed to board_type/board_id.',
    )
    action: Literal["set_rate", "clear"] = Field(
        description="set_rate = set SD log decimation divisor, clear = reformat the card",
    )
    divisor: int = Field(
        default=1, ge=1, le=255,
        description="Log decimation divisor for action=set_rate (1 = full rate)",
    )


class FasRabPayload(BaseModel):
    """Recovery Arming Board (RAB) arm/disarm. Safety-critical: the FMC pulses the
    addressed RAB's GPIO_ARM / GPIO_DISARM. rab_id selects the unit (0 = A, 1 = B)."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"action": "arm", "rab_id": 0, "pulse_ms": 100}}
    )

    action: Literal["arm", "disarm"] = Field(
        description="arm = pulse GPIO_ARM, disarm = pulse GPIO_DISARM on the addressed RAB",
    )
    rab_id: Literal[0, 1] = Field(description="RAB unit: 0 = A, 1 = B")
    pulse_ms: int = Field(
        default=100, ge=1, le=1000,
        description="Momentary arm/disarm pulse length in milliseconds (spec default 100)",
    )


class FasAuxPayload(BaseModel):
    """FMC auxiliary load-switch power: RFD900x radio or RunCam, on/off."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"node": "FMC_0", "device": "runcam", "enable": True}}
    )

    node: str | None = Field(
        default=None,
        description='FAS board node string, e.g. "FMC_0". Parsed to board_type/board_id.',
    )
    device: Literal["rfd", "runcam"] = Field(description="Load switch to control")
    enable: bool = Field(description="True = power on, False = power off")


class FasRfPayload(BaseModel):
    """FMC RF telemetry rate/power mode. The mode is PERSISTED ON THE FMC; only send
    this on an explicit operator change (the FMC's LOW default is authoritative)."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"node": "FMC_0", "mode": 0}}
    )

    node: str | None = Field(
        default=None,
        description='FAS board node string, e.g. "FMC_0". Parsed to board_type/board_id.',
    )
    mode: Literal[0, 1, 2] = Field(
        description="RF telemetry rate/power mode: 0 = low (default, power-saving), 1 = normal, 2 = high",
    )


class FasSoundPayload(BaseModel):
    """Soundboard control (replaces the removed FMC buzzer). play/stop/volume/tone/
    list/clear; clip upload streaming is not handled through this endpoint."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"node": "FMC_0", "action": "tone", "freq_hz": 2000, "ms": 300}}
    )

    node: str | None = Field(
        default=None,
        description='FAS board node string, e.g. "FMC_0". Parsed to board_type/board_id.',
    )
    action: Literal["play", "stop", "volume", "tone", "list", "clear"] = Field(
        description="play (idx) / stop / volume (0..255) / tone (freq_hz,ms) / list / clear all clips",
    )
    idx: int = Field(default=0, ge=0, le=255, description="Clip index for action=play")
    volume: int = Field(default=255, ge=0, le=255, description="Digital volume for action=volume")
    freq_hz: int = Field(default=2000, ge=0, le=20000, description="Tone frequency Hz (0 = default) for action=tone")
    ms: int = Field(default=500, ge=0, le=60000, description="Tone duration ms (0 = default) for action=tone")


class FasChargerPayload(BaseModel):
    """PMB battery charging control. Charging is DEFAULT-OFF; this enables or
    suspends it and optionally sets the LTC4162 current/voltage limit DAC codes.
    Omit i_setting/v_setting to leave the persisted limits unchanged."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"enable": True, "i_setting": 16, "v_setting": 20}}
    )

    node: str | None = Field(
        default=None,
        description='FAS board node string, e.g. "PMB_0". Parsed to board_type/board_id.',
    )
    enable: bool = Field(description="True = allow charging, False = suspend")
    i_setting: int | None = Field(
        default=None, ge=0, le=31,
        description="Charge-current DAC code (0..31); omit to leave unchanged",
    )
    v_setting: int | None = Field(
        default=None, ge=0, le=31,
        description="Charge-voltage DAC code (0..31); omit to leave unchanged",
    )


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
