from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class ActuatorType(str, Enum):
    SOLENOID = "solenoid"
    SERVO = "servo"
    SERVO3 = "servo3"
    POWERED_DEVICE = "poweredDevice"
    POWERED_GPIO_DEVICE = "poweredGpioDevice"


class SensorConfig(BaseModel):
    hat_id: int = Field(validation_alias=AliasChoices("hatID", "hatId", "hat_id"), serialization_alias="hatID")
    channel_id: int = Field(validation_alias=AliasChoices("channelID", "channelId", "channel_id"), serialization_alias="channelID")
    name: str
    unit: str = ""
    calibration: list[tuple[float, float]] = Field(default_factory=list)


class ActuatorConfig(BaseModel):
    channel_id: int = Field(validation_alias=AliasChoices("channelID", "channelId", "channel_id"), serialization_alias="channelID")
    name: str
    actuator_type: ActuatorType = Field(validation_alias=AliasChoices("actuator_type", "actuatorType"))
    relay_type: str | None = None
    solenoid_type: str | None = None
    relay_id: int | None = Field(default=None, validation_alias=AliasChoices("relayID", "relayId", "relay_id"), serialization_alias="relayID")
    position_aliases: list[str] = Field(default_factory=list)
    positions: list[int] = Field(default_factory=list)
    default_position: str | int | None = Field(default=None, validation_alias=AliasChoices("default_position", "defaultPosition"), serialization_alias="defaultPosition")

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_actuator_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        normalized = dict(data)
        actuator_type_hint = str(
            normalized.get("actuator_type", normalized.get("actuatorType", ""))
        ).strip().lower()

        if "solenoid_type" not in normalized:
            if "solenoidType" in normalized:
                normalized["solenoid_type"] = normalized["solenoidType"]
            elif actuator_type_hint == "solenoid" and "type" in normalized:
                normalized["solenoid_type"] = normalized["type"]

        if "relay_type" not in normalized:
            if "relayType" in normalized:
                normalized["relay_type"] = normalized["relayType"]
            elif "type" in normalized:
                normalized["relay_type"] = normalized["type"]

        if "position_aliases" not in normalized and "positionAliases" in normalized:
            normalized["position_aliases"] = normalized["positionAliases"]

        return normalized

    @model_validator(mode="after")
    def validate_positions(self) -> "ActuatorConfig":
        if self.position_aliases and len(self.position_aliases) != len(self.positions):
            raise ValueError("position_aliases and positions must have the same size")
        return self


class SystemConfig(BaseModel):
    mcc128daq: list[SensorConfig] = Field(default_factory=list, alias="MCC128DAQ")
    mcc134daq: list[SensorConfig] = Field(default_factory=list, alias="MCC134DAQ")
    fas: list[SensorConfig] = Field(default_factory=list, alias="FAS")
    mccdaq: list[SensorConfig] = Field(default_factory=list, alias="MCCDAQ")

    relay_board: list[ActuatorConfig] = Field(default_factory=list, alias="relayBoard")
    pca9685: list[ActuatorConfig] = Field(default_factory=list, alias="PCA9685")


    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_config(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        pca_entries = data.get("PCA9685")
        if not isinstance(pca_entries, list):
            return data

        normalized: list[Any] = []
        for entry in pca_entries:
            if not isinstance(entry, dict):
                normalized.append(entry)
                continue

            if "actuator_type" not in entry:
                aliases = entry.get("position_aliases") or entry.get("positionAliases") or []
                positions = entry.get("positions") or []
                count = max(len(aliases), len(positions))
                inferred = "servo3" if count > 2 else "servo"
                entry = {**entry, "actuator_type": inferred}

            normalized.append(entry)

        return {**data, "PCA9685": normalized}

    def all_sensors(self) -> list[SensorConfig]:
        sensors = []
        sensors.extend(self.mcc128daq)
        sensors.extend(self.mcc134daq)
        sensors.extend(self.fas)
        sensors.extend(self.mccdaq)
        return sensors

    def all_actuators(self) -> list[ActuatorConfig]:
        actuators = []
        actuators.extend(self.relay_board)
        actuators.extend(self.pca9685)
        return actuators


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


class IncomingSensorPacket(BaseModel):
    source: str
    sensors: list[dict[str, Any]]
