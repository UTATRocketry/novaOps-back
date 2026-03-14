from app.models import CommandPayload, SystemConfig
from app.parsing import CommandParser


def test_solenoid_command_translates_to_relay_state() -> None:
    config = SystemConfig.model_validate(
        {
            "relayBoard": [
                {
                    "channelID": 0,
                    "name": "SVOTV",
                    "actuator_type": "solenoid",
                    "relay_type": "NO",
                    "solenoid_type": "NC",
                }
            ]
        }
    )
    parser = CommandParser(config)

    translated = parser.parse(CommandPayload(type="solenoid", name="SVOTV", state="open"))

    assert translated == [{"type": "relay", "id": 0, "state": 0}]


def test_servo_alias_translates_to_angle() -> None:
    config = SystemConfig.model_validate(
        {
            "PCA9685": [
                {
                    "channelID": 1,
                    "name": "BVOT3",
                    "actuator_type": "servo3",
                    "position_aliases": ["N", "F", "D"],
                    "positions": [1800, 1200, 600],
                }
            ]
        }
    )
    parser = CommandParser(config)

    translated = parser.parse(CommandPayload(type="servo3", name="BVOT3", state="F"))

    assert translated == [{"type": "servo", "id": 1, "angle": 1200}]


def test_powered_gpio_arm_translates_to_gpio_state() -> None:
    config = SystemConfig.model_validate(
        {
            "relayBoard": [
                {
                    "channelID": 9,
                    "name": "IGNITER_ARM",
                    "actuator_type": "poweredGpioDevice"
                }
            ]
        }
    )
    parser = CommandParser(config)

    translated = parser.parse(CommandPayload(type="poweredGpioDevice", name="IGNITER_ARM", state="armed"))

    assert translated == [{"type": "gpio", "id": 9, "state": 0}]


def test_servo_on_off_without_relay_id_raises() -> None:
    config = SystemConfig.model_validate(
        {
            "PCA9685": [
                {
                    "channelID": 4,
                    "name": "BVOTP",
                    "actuator_type": "servo",
                    "position_aliases": ["open", "closed"],
                    "positions": [1000, 1900]
                }
            ]
        }
    )
    parser = CommandParser(config)

    try:
        parser.parse(CommandPayload(type="servo", name="BVOTP", state="on"))
        assert False, "Expected ValueError"
    except ValueError as exc:
        assert "does not define relayID" in str(exc)



def test_solenoid_mapping_respects_legacy_polarity_keys() -> None:
    config = SystemConfig.model_validate(
        {
            "relayBoard": [
                {
                    "channelID": 2,
                    "name": "SV_TEST",
                    "actuator_type": "solenoid",
                    "type": "NC",
                    "solenoidType": "NC",
                }
            ]
        }
    )
    parser = CommandParser(config)

    translated_open = parser.parse(CommandPayload(type="solenoid", name="SV_TEST", state="open"))
    translated_closed = parser.parse(CommandPayload(type="solenoid", name="SV_TEST", state="closed"))

    assert translated_open == [{"type": "relay", "id": 2, "state": 1}]
    assert translated_closed == [{"type": "relay", "id": 2, "state": 0}]


def test_solenoid_legacy_type_key_is_used_for_solenoid_polarity() -> None:
    config = SystemConfig.model_validate(
        {
            "relayBoard": [
                {
                    "channelID": 3,
                    "name": "SV_LEGACY",
                    "actuator_type": "solenoid",
                    "relay_type": "NO",
                    "type": "NO",
                }
            ]
        }
    )
    parser = CommandParser(config)

    translated_open = parser.parse(CommandPayload(type="solenoid", name="SV_LEGACY", state="open"))
    translated_closed = parser.parse(CommandPayload(type="solenoid", name="SV_LEGACY", state="closed"))

    assert translated_open == [{"type": "relay", "id": 3, "state": 1}]
    assert translated_closed == [{"type": "relay", "id": 3, "state": 0}]
