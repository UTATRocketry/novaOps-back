import pytest

from app.models import CommandPayload, SystemConfig
from app.parsing import CommandParser


def _config(actuators: list[dict], commands: dict | None = None) -> SystemConfig:
    payload: dict = {"Actuators": actuators}
    if commands is not None:
        payload["Commands"] = commands
    return SystemConfig.model_validate(payload)


# --- GCS path: dict output unchanged from legacy shape ---

def test_gcs_solenoid_translates_to_relay_state() -> None:
    config = _config(
        [
            {
                "name": "SVBVGS",
                "type": "solenoid",
                "binding": {"target": "GCS", "relay_channel": 8},
                "actions": {"relay_type": "nominally_off", "solenoid_type": "nominally_closed"},
            }
        ]
    )
    parser = CommandParser(config)

    assert parser.parse(CommandPayload(type="solenoid", name="SVBVGS", state="open")) == [
        {"type": "relay", "id": 8, "state": 0}
    ]
    assert parser.parse(CommandPayload(type="solenoid", name="SVBVGS", state="closed")) == [
        {"type": "relay", "id": 8, "state": 1}
    ]


def test_gcs_servo_alias_translates_to_angle() -> None:
    config = _config(
        [
            {
                "name": "BVGSO",
                "type": "servo",
                "binding": {"target": "GCS", "relay_channel": 2, "servo_channel": 8},
                "actions": {"position_aliases": ["open", "closed"], "positions": [900, 1900]},
            }
        ]
    )
    parser = CommandParser(config)

    assert parser.parse(CommandPayload(type="servo", name="BVGSO", state="open")) == [
        {"type": "servo", "id": 8, "angle": 900}
    ]


def test_gcs_servo_power_uses_relay_channel() -> None:
    config = _config(
        [
            {
                "name": "BVGSO",
                "type": "servo",
                "binding": {"target": "GCS", "relay_channel": 2, "servo_channel": 8},
                "actions": {"position_aliases": ["open", "closed"], "positions": [900, 1900]},
            }
        ]
    )
    parser = CommandParser(config)

    assert parser.parse(CommandPayload(type="servo", name="BVGSO", state="on")) == [
        {"type": "relay", "id": 2, "state": 0}
    ]


def test_gcs_servo_on_without_relay_channel_raises() -> None:
    config = _config(
        [
            {
                "name": "NOPWR",
                "type": "servo",
                "binding": {"target": "GCS", "servo_channel": 8},
                "actions": {"position_aliases": ["open", "closed"], "positions": [900, 1900]},
            }
        ]
    )
    parser = CommandParser(config)

    with pytest.raises(ValueError, match="does not define relay_channel"):
        parser.parse(CommandPayload(type="servo", name="NOPWR", state="on"))


# --- FAS path: abstract dict output ---

def test_fas_solenoid_emits_abstract_relay_dict() -> None:
    config = _config(
        [
            {
                "name": "SVFTV",
                "type": "solenoid",
                "binding": {"target": "FAS", "node": "EPB_1", "relay_channel": 2},
                "actions": {"relay_type": "nominally_off", "solenoid_type": "nominally_closed"},
            }
        ]
    )
    parser = CommandParser(config)

    assert parser.parse(CommandPayload(type="solenoid", name="SVFTV", state="open")) == [
        {"type": "fas", "node": "EPB_1", "port": "relay", "channel": 2, "action": "on"}
    ]
    assert parser.parse(CommandPayload(type="solenoid", name="SVFTV", state="closed")) == [
        {"type": "fas", "node": "EPB_1", "port": "relay", "channel": 2, "action": "off"}
    ]


def test_fas_servo_emits_relay_then_servo_sequence() -> None:
    config = _config(
        [
            {
                "name": "BVFTP",
                "type": "servo",
                "binding": {"target": "FAS", "node": "EPB_1", "relay_channel": 1, "servo_channel": 1},
                "actions": {"position_aliases": ["open", "closed"], "positions": [2050, 1090]},
            }
        ]
    )
    parser = CommandParser(config)

    assert parser.parse(CommandPayload(type="servo", name="BVFTP", state="open")) == [
        {"type": "fas", "node": "EPB_1", "port": "relay", "channel": 1, "action": "on"},
        {"type": "fas", "node": "EPB_1", "port": "servo", "channel": 1, "action": "open", "value": 2050},
    ]


def test_fas_powered_gpio_emits_gpio_action() -> None:
    config = _config(
        [
            {
                "name": "IMC-V",
                "type": "powered_gpio_device",
                "binding": {"target": "FAS", "node": "EPB_3", "relay_channel": 2},
                "actions": {"relay_type": "nominally_off", "gpio_commands": ["ARM", "DISARM"]},
            }
        ]
    )
    parser = CommandParser(config)

    assert parser.parse(CommandPayload(type="powered_gpio_device", name="IMC-V", state="ARM")) == [
        {"type": "fas", "node": "EPB_3", "port": "gpio", "channel": 2, "action": "ARM"}
    ]


# --- System (Commands section) ---

def test_system_command_emits_fas_cmd_with_validated_state() -> None:
    config = _config(
        [],
        commands={
            "SET_FLIGHT_STATE": {
                "binding": {"target": "FAS", "node": "FMC"},
                "states": ["STANDBY", "ARMED"],
            }
        },
    )
    parser = CommandParser(config)

    assert parser.parse_system_command("SET_FLIGHT_STATE", "ARMED") == [
        {"type": "fas_cmd", "node": "FMC", "command": "SET_FLIGHT_STATE", "state": "ARMED"}
    ]


def test_system_command_rejects_invalid_state() -> None:
    config = _config(
        [],
        commands={
            "SET_FLIGHT_STATE": {
                "binding": {"target": "FAS", "node": "FMC"},
                "states": ["STANDBY", "ARMED"],
            }
        },
    )
    parser = CommandParser(config)

    with pytest.raises(ValueError, match="requires state"):
        parser.parse_system_command("SET_FLIGHT_STATE", "LAUNCH")
