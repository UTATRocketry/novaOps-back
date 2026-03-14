from app.models import SystemConfig
from app.parsing import RollingAverageStore, SensorParser


def test_sensor_parser_maps_sensor_and_calibrates() -> None:
    config = SystemConfig.model_validate(
        {
            "MCC128DAQ": [
                {
                    "hatID": 0,
                    "channelID": 0,
                    "name": "POT",
                    "unit": "psi",
                    "calibration": [[0, 0], [2, 100]],
                }
            ]
        }
    )

    parser = SensorParser(RollingAverageStore(window_size=5))
    parsed = parser.parse(
        source="novaGround",
        raw_sensors=[{"hat_id": 0, "channel_id": 0, "value": 1.0, "timestamp": 123}],
        config=config,
        calibration_enabled=True,
    )

    assert len(parsed) == 1
    assert parsed[0].name == "POT"
    assert parsed[0].unit == "psi"
    assert parsed[0].value == 50.0
