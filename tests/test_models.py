import pytest
from pydantic import ValidationError

from app.models import (
    ActuatorType,
    ConvertMethod,
    FasSensorBinding,
    GcsSensorBinding,
    SensorType,
    SourceTarget,
    SystemConfig,
)


def _gcs_sensor() -> dict:
    return {
        "name": "PGSO",
        "type": "PT",
        "unit": "psi",
        "binding": {"source": "GCS", "hat_id": 0, "channel_id": 0},
        "convert": {"method": "linear", "calibration": [[0.99, 0], [4.08, 775]]},
    }


def _fas_sensor() -> dict:
    return {
        "name": "PFT",
        "type": "PT",
        "unit": "psi",
        "binding": {"source": "FAS", "node": "EPB_1", "channel": 1},
        "convert": {"method": "linear", "calibration": [[1, 1]]},
    }


def test_sensor_binding_discriminates_on_source() -> None:
    config = SystemConfig.model_validate({"Sensors": [_gcs_sensor(), _fas_sensor()]})

    gcs, fas = config.sensors
    assert isinstance(gcs.binding, GcsSensorBinding)
    assert gcs.binding.hat_id == 0 and gcs.binding.channel_id == 0
    assert gcs.type == SensorType.PT
    assert isinstance(fas.binding, FasSensorBinding)
    assert fas.binding.node == "EPB_1" and fas.binding.channel == 1


def test_fas_sensor_binding_derives_node_from_board_fields() -> None:
    config = SystemConfig.model_validate(
        {
            "Sensors": [
                {
                    "name": "POT",
                    "type": "PT",
                    "unit": "psi",
                    "binding": {"source": "FAS", "board_type": "EPB", "board_id": 1, "channel": 1},
                    "convert": {"method": "linear", "calibration": [[1, 1]]},
                }
            ]
        }
    )

    binding = config.sensors[0].binding
    assert isinstance(binding, FasSensorBinding)
    assert binding.resolved_node == "EPB_2"


def test_tc_sensor_allows_convert_none() -> None:
    config = SystemConfig.model_validate(
        {
            "Sensors": [
                {
                    "name": "TOT",
                    "type": "TC",
                    "unit": "degC",
                    "binding": {"source": "TCS", "hat_id": 0, "channel_id": 0},
                    "convert": {"method": "none"},
                }
            ]
        }
    )
    assert config.sensors[0].convert.method == ConvertMethod.NONE


def test_actuator_types_are_lowercase() -> None:
    config = SystemConfig.model_validate(
        {
            "Actuators": [
                {
                    "name": "SVBVGS",
                    "type": "solenoid",
                    "binding": {"target": "GCS", "relay_channel": 8},
                    "actions": {"relay_type": "nominally_off", "solenoid_type": "nominally_closed"},
                }
            ]
        }
    )
    actuator = config.actuators[0]
    assert actuator.type == ActuatorType.SOLENOID
    assert actuator.binding.target == SourceTarget.GCS
    assert actuator.binding.relay_channel == 8


def test_gpio_device_type_is_supported() -> None:
    config = SystemConfig.model_validate(
        {
            "Actuators": [
                {
                    "name": "IMC-V",
                    "type": "gpio_device",
                    "binding": {"target": "FAS", "node": "EPB_3", "gpio_channel": 2},
                    "actions": {"gpio_commands": ["ARM", "DISARM"]},
                }
            ]
        }
    )

    actuator = config.actuators[0]
    assert actuator.type == ActuatorType.GPIO_DEVICE
    assert actuator.binding.target == SourceTarget.FAS
    assert actuator.binding.node == "EPB_3"
    assert actuator.binding.gpio_channel == 2
    assert actuator.actions.gpio_commands == ["ARM", "DISARM"]


def test_motor_type_defaults_labels_and_neutral_state() -> None:
    config = SystemConfig.model_validate(
        {
            "Actuators": [
                {
                    "name": "LINACT",
                    "type": "motor",
                    "binding": {"target": "GCS", "relay_channel": 3, "reverse_relay_channel": 4},
                    "actions": {"reversible": True},
                },
                {
                    "name": "PUMP",
                    "type": "motor",
                    "binding": {"target": "GCS", "relay_channel": 7},
                },
            ]
        }
    )

    linear, pump = config.actuators
    assert linear.type == ActuatorType.MOTOR
    assert linear.motor_labels == ["forward", "stop", "reverse"]
    assert linear.motor_channels == [3, 4]
    assert linear.motor_neutral_label == "stop"
    assert linear.resolve_motor_state("reverse") == [0, 1]

    assert pump.motor_labels == ["on", "off"]
    assert pump.motor_channels == [7]
    assert pump.motor_neutral_label == "off"


def test_reversible_motor_requires_second_relay_channel() -> None:
    with pytest.raises(ValidationError, match="reverse_relay_channel"):
        SystemConfig.model_validate(
            {
                "Actuators": [
                    {
                        "name": "LINACT",
                        "type": "motor",
                        "binding": {"target": "GCS", "relay_channel": 3},
                        "actions": {"reversible": True},
                    }
                ]
            }
        )


def test_motor_state_labels_must_match_pattern_count() -> None:
    with pytest.raises(ValidationError, match="needs exactly 3 state_labels"):
        SystemConfig.model_validate(
            {
                "Actuators": [
                    {
                        "name": "LINACT",
                        "type": "motor",
                        "binding": {"target": "GCS", "relay_channel": 3, "reverse_relay_channel": 4},
                        "actions": {"reversible": True, "state_labels": ["extend", "retract"]},
                    }
                ]
            }
        )


def test_servo_default_position_alias_accepts_camel_case() -> None:
    config = SystemConfig.model_validate(
        {
            "Actuators": [
                {
                    "name": "BVGSO",
                    "type": "servo",
                    "binding": {"target": "GCS", "relay_channel": 2, "servo_channel": 8},
                    "actions": {
                        "position_aliases": ["open", "closed"],
                        "positions": [900, 1900],
                        "defaultPosition": "closed",
                    },
                }
            ]
        }
    )
    assert config.actuators[0].actions.default_position == "closed"


def test_fas_actuator_without_node_raises() -> None:
    with pytest.raises(ValueError, match=r"must set either binding.node \(legacy\) or binding.board_type"):
        SystemConfig.model_validate(
            {
                "Actuators": [
                    {
                        "name": "SVFTV",
                        "type": "solenoid",
                        "binding": {"target": "FAS", "relay_channel": 2},
                    }
                ]
            }
        )


def test_position_aliases_length_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="length mismatch"):
        SystemConfig.model_validate(
            {
                "Actuators": [
                    {
                        "name": "BAD",
                        "type": "servo",
                        "binding": {"target": "GCS", "servo_channel": 1},
                        "actions": {"position_aliases": ["open", "closed"], "positions": [900]},
                    }
                ]
            }
        )


def test_command_section_parses_with_states() -> None:
    config = SystemConfig.model_validate(
        {
            "Commands": {
                "SET_FLIGHT_STATE": {
                    "binding": {"target": "FAS", "node": "FMC"},
                    "states": ["STANDBY", "ARMED"],
                },
                "START_DATA_SAVING": {"binding": {"target": "GCS"}},
            }
        }
    )
    command = config.find_command("SET_FLIGHT_STATE")
    assert command is not None
    assert command.binding.node == "FMC"
    assert command.states == ["STANDBY", "ARMED"]
    assert config.find_command("START_DATA_SAVING").states is None


def test_safety_rules_match_hazardous_commands() -> None:
    config = SystemConfig.model_validate(
        {
            "safetyRules": {
                "hazardous": [
                    {"IMC-V": "ALL"},
                    {"BVGSO": "OPEN"},
                    {"BVFTP": ["OPEN", "CLOSE"]},
                ]
            }
        }
    )

    assert config.is_hazardous_command("IMC-V", "DISARM")
    assert config.is_hazardous_command("BVGSO", "open")
    assert config.is_hazardous_command("BVFTP", "close")
    assert not config.is_hazardous_command("BVGSO", "closed")
    assert not config.is_hazardous_command("SVFTV", "open")
