from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class SourceTarget(str, Enum):
    GCS = "GCS"
    FAS = "FAS"
    TCS = "TCS"


class SensorType(str, Enum):
    PT = "PT"
    LC = "LC"
    TC = "TC"


class ActuatorType(str, Enum):
    SERVO = "servo"
    SOLENOID = "solenoid"
    POWERED_DEVICE = "powered_device"
    POWERED_GPIO_DEVICE = "powered_gpio_device"


class ConvertMethod(str, Enum):
    NONE = "none"
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
    node: str
    channel: int


class SensorEntry(BaseModel):
    name: str
    type: SensorType
    unit: str = ""
    binding: GcsSensorBinding | FasSensorBinding = Field(discriminator="source")
    convert: ConvertSpec = Field(default_factory=ConvertSpec)


class ActuatorBinding(BaseModel):
    target: SourceTarget
    node: str | None = None  # required when target == FAS
    relay_channel: int | None = None
    servo_channel: int | None = None


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
    def _fas_needs_node(self) -> "ActuatorEntry":
        if self.binding.target == SourceTarget.FAS and self.binding.node is None:
            raise ValueError(f"FAS actuator '{self.name}' must set binding.node")
        return self


class CommandBinding(BaseModel):
    target: SourceTarget
    node: str | None = None
    channel: int | None = None


class CommandEntry(BaseModel):
    binding: CommandBinding
    states: list[str] | None = None  # e.g. [STANDBY, ARMED]


class SystemConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    sensors: list[SensorEntry] = Field(default_factory=list, alias="Sensors")
    actuators: list[ActuatorEntry] = Field(default_factory=list, alias="Actuators")
    commands: dict[str, CommandEntry] = Field(default_factory=dict, alias="Commands")

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


class IncomingSensorPacket(BaseModel):
    source: str
    sensors: list[dict[str, Any]]
