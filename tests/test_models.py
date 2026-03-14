from app.models import ActuatorType, SystemConfig


def test_pca9685_missing_actuator_type_defaults_to_servo() -> None:
    config = SystemConfig.model_validate(
        {
            "PCA9685": [
                {
                    "channelID": 4,
                    "name": "BVOTP",
                    "position_aliases": ["open", "closed"],
                    "positions": [1300, 1900],
                    "relayID": 4,
                }
            ]
        }
    )

    assert config.pca9685[0].actuator_type == ActuatorType.SERVO


def test_pca9685_missing_actuator_type_defaults_to_servo3() -> None:
    config = SystemConfig.model_validate(
        {
            "PCA9685": [
                {
                    "channelID": 10,
                    "name": "BVOT3",
                    "position_aliases": ["N", "F", "D"],
                    "positions": [1200, 900, 600],
                    "relayID": 7,
                }
            ]
        }
    )

    assert config.pca9685[0].actuator_type == ActuatorType.SERVO3


def test_pca9685_relay_id_aliases_are_accepted() -> None:
    config = SystemConfig.model_validate(
        {
            "PCA9685": [
                {
                    "channelID": 4,
                    "name": "BVOTP",
                    "position_aliases": ["open", "closed"],
                    "positions": [1000, 1900],
                    "relayId": 12,
                }
            ]
        }
    )

    assert config.pca9685[0].relay_id == 12
    dumped = config.pca9685[0].model_dump(by_alias=True)
    assert dumped["relayID"] == 12
