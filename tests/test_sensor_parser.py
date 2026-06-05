from app.models import SystemConfig
from app.parsing import RollingAverageStore, SensorParser


def _config() -> SystemConfig:
    return SystemConfig.model_validate(
        {
            "Sensors": [
                {
                    "name": "PGSO",
                    "type": "PT",
                    "unit": "psi",
                    "binding": {"source": "GCS", "hat_id": 0, "channel_id": 0},
                    "convert": {"method": "linear", "calibration": [[0, 0], [2, 100]]},
                },
                {
                    "name": "TOT",
                    "type": "TC",
                    "unit": "degC",
                    "binding": {"source": "TCS", "hat_id": 0, "channel_id": 0},
                    "convert": {"method": "none"},
                },
                {
                    "name": "POT",
                    "type": "PT",
                    "unit": "psi",
                    "binding": {"source": "FAS", "node": "EPB_2", "channel": 1},
                    "convert": {"method": "linear", "calibration": [[0, 0], [2, 100]]},
                },
            ]
        }
    )


def test_gcs_sensor_maps_and_calibrates() -> None:
    parser = SensorParser(RollingAverageStore(window_size=5))
    parsed = parser.parse(
        source="GCS",
        raw_sensors=[{"hat_id": 0, "channel_id": 0, "value": 1.0, "timestamp": 123}],
        config=_config(),
        calibration_enabled=True,
    )

    assert len(parsed) == 1
    assert parsed[0].name == "PGSO"
    assert parsed[0].unit == "psi"
    assert parsed[0].value == 50.0


def test_fas_sensor_maps_on_node_and_channel() -> None:
    parser = SensorParser(RollingAverageStore(window_size=5))
    parsed = parser.parse(
        source="FAS",
        raw_sensors=[{"node": "EPB_2", "channel": 1, "value": 1.0, "timestamp": 9}],
        config=_config(),
        calibration_enabled=True,
    )

    assert len(parsed) == 1
    assert parsed[0].name == "POT"
    assert parsed[0].value == 50.0


def test_convert_none_passes_raw_value_through() -> None:
    parser = SensorParser(RollingAverageStore(window_size=5))
    parsed = parser.parse(
        source="TCS",
        raw_sensors=[{"hat_id": 0, "channel_id": 0, "value": 25.5, "timestamp": 1}],
        config=_config(),
        calibration_enabled=True,
    )

    assert len(parsed) == 1
    assert parsed[0].name == "TOT"
    assert parsed[0].value == 25.5


def test_source_filters_out_other_buses() -> None:
    parser = SensorParser(RollingAverageStore(window_size=5))
    # A GCS-addressed packet must not match the FAS sensor on the same channel id.
    parsed = parser.parse(
        source="GCS",
        raw_sensors=[{"hat_id": 9, "channel_id": 9, "value": 1.0, "timestamp": 1}],
        config=_config(),
        calibration_enabled=True,
    )
    assert parsed == []
