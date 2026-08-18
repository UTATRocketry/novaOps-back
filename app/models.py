from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

# Console actions the FAS bridge accepts on nova/command (see tools/fas_bridge.py
# _cmd_console). "configure"/"disconnect" set or drop the bridge's serial port,
# "status" asks it to republish the link state.
CONSOLE_ACTIONS = frozenset(
    {"start", "stop", "list_ports", "configure", "disconnect", "status", "tx"}
)


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
    MOTOR = "motor"


# Relay patterns for `motor` actuators, one bit per driven relay channel, in the
# same order as the type's state labels.
#
# A reversible motor is wired through two relays in a reverse-polarity pair, so
# the middle (all-off) state is what leaves the motor coasting and is the only
# safe state to pass through when changing direction.
MOTOR_PATTERNS_REVERSIBLE = ([1, 0], [0, 0], [0, 1])
MOTOR_PATTERNS_ONE_WAY = ([1], [0])
MOTOR_LABELS_REVERSIBLE = ("forward", "stop", "reverse")
MOTOR_LABELS_ONE_WAY = ("on", "off")


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
    # Second relay of a reversible motor's reverse-polarity pair. `relay_channel`
    # is the forward leg, this one the reverse leg.
    reverse_relay_channel: int | None = None


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
    # motor
    reversible: bool = Field(
        default=False,
        description="Motor is driven by two relays wired for reverse polarity",
    )
    state_labels: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("stateLabels", "state_labels"),
        description=(
            "Motor state names, one per relay pattern: [forward, stop, reverse] "
            "when reversible, [on, off] otherwise. Defaults to those names."
        ),
    )
    invert_relays: bool = Field(
        default=False,
        description="Motor only — publish the complement of each relay pattern bit "
        "(for active-low relay boards)",
    )

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

    @model_validator(mode="after")
    def _motor_is_wired(self) -> "ActuatorEntry":
        if self.type != ActuatorType.MOTOR:
            return self

        if self.binding.relay_channel is None:
            raise ValueError(f"Motor '{self.name}' must define binding.relay_channel")
        if self.actions.reversible and self.binding.reverse_relay_channel is None:
            raise ValueError(
                f"Reversible motor '{self.name}' must define binding.reverse_relay_channel"
            )

        labels = self.actions.state_labels
        expected = len(self.motor_patterns)
        if labels and len(labels) != expected:
            raise ValueError(
                f"Motor '{self.name}' needs exactly {expected} state_labels "
                f"({'reversible' if self.actions.reversible else 'one-way'}), got {len(labels)}"
            )
        lowered = [label.strip().lower() for label in labels]
        if len(set(lowered)) != len(lowered):
            raise ValueError(f"Motor '{self.name}' has duplicate state_labels")
        return self

    @property
    def motor_patterns(self) -> tuple[list[int], ...]:
        return MOTOR_PATTERNS_REVERSIBLE if self.actions.reversible else MOTOR_PATTERNS_ONE_WAY

    @property
    def motor_labels(self) -> list[str]:
        if self.actions.state_labels:
            return list(self.actions.state_labels)
        return list(MOTOR_LABELS_REVERSIBLE if self.actions.reversible else MOTOR_LABELS_ONE_WAY)

    @property
    def motor_channels(self) -> list[int]:
        """Relay channels this motor drives, in relay-pattern bit order."""
        channels = [self.binding.relay_channel]
        if self.actions.reversible:
            channels.append(self.binding.reverse_relay_channel)
        return [channel for channel in channels if channel is not None]

    @property
    def motor_neutral_label(self) -> str:
        """The label whose pattern de-energizes every relay (stop / off)."""
        for label, pattern in zip(self.motor_labels, self.motor_patterns):
            if not any(pattern):
                return label
        return self.motor_labels[-1]

    def resolve_motor_state(self, state: str) -> list[int]:
        """Map a requested state label to its energization pattern. 1 means "this
        leg is driven"; `actions.invert_relays` is applied later, when the bits are
        turned into wire-level relay states."""
        requested = state.strip().lower()
        for label, pattern in zip(self.motor_labels, self.motor_patterns):
            if label.strip().lower() == requested:
                return list(pattern)
        raise ValueError(
            f"Unsupported motor state '{state}' for actuator '{self.name}' "
            f"(expected one of {', '.join(self.motor_labels)})"
        )


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
    """FMC auxiliary rail power. Only "radio" (the STM32WL modem) is an FMC pin;
    "runcam" and "rf_pa" are EPB load switches the FMC is the single writer for.
    "rfd" is a deprecated alias for "radio", kept so an older client keeps
    addressing the vehicle modem rather than device 0 by coincidence."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"node": "FMC_0", "device": "runcam", "enable": True}}
    )

    node: str | None = Field(
        default=None,
        description='FAS board node string, e.g. "FMC_0". Parsed to board_type/board_id.',
    )
    device: Literal["radio", "runcam", "rf_pa", "rfd"] = Field(
        description=(
            "Rail to control: radio (STM32WL modem, FMC pin), runcam (EPB load "
            "switch), rf_pa (EPB load switch for the RF amplifier). rfd is a "
            "deprecated alias for radio."
        ),
    )
    enable: bool = Field(description="True = power on, False = power off")


class FasRuncamRecordPayload(BaseModel):
    """RunCam Device Protocol record start/stop. Distinct from powering the
    camera rail: with the firmware's rec_on_power default, bringing the rail up
    already starts a recording, so this is for explicit control."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"node": "FMC_0", "enable": True, "autostop_s": 1800}}
    )

    node: str | None = Field(
        default=None,
        description='FAS board node string, e.g. "FMC_0". Parsed to board_type/board_id.',
    )
    enable: bool = Field(description="True = start recording, False = stop")
    autostop_s: int = Field(
        default=0, ge=0, le=43200,
        description=(
            "Auto-stop timeout in seconds; 0 = record until stopped. The wire "
            "field is 16-bit and the firmware caps it at 43200 (12 h)."
        ),
    )


class FasRadioConfigPayload(BaseModel):
    """The complete FMC-authoritative vehicle-radio configuration, carried as one
    88-byte bulk record. Wired link only — the firmware never accepts it over RF.

    `cfg` is passed through to the bridge unvalidated here on purpose: the bridge
    owns the byte-level bounds (they mirror radio_config_store.h) and rejects a
    bad record with a logged reason, so duplicating them here would mean two
    copies to keep in step with the firmware."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "node": "FMC_0",
                "action": "set",
                "cfg": {
                    "callsign": "VA3UTA",
                    "allocation_low_hz": 908200000,
                    "allocation_high_hz": 909000000,
                    "lora": {
                        "frequency_hz": 908600000, "bandwidth_hz": 500000,
                        "power_dbm": 14, "spreading_factor": 12,
                        "coding_rate": "4/5", "preamble_symbols": 8,
                    },
                },
            }
        }
    )

    node: str | None = Field(
        default=None,
        description='FAS board node string, e.g. "FMC_0". Parsed to board_type/board_id.',
    )
    action: Literal["set", "get"] = Field(
        default="set",
        description="set = write and persist on the FMC, get = request a read-back",
    )
    transaction_id: int = Field(
        default=0, ge=0, le=0xFFFFFFFF,
        description="Echoed in the FMC's read-back so a reply can be matched to its request",
    )
    cfg: dict = Field(
        description=(
            "Complete radio configuration object: callsign, allocation_low_hz, "
            "allocation_high_hz, lora{...}, pressure_channels[], rf_chain{...}"
        ),
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
