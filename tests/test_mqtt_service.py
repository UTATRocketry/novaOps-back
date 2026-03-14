from app.mqtt_service import MqttService


def test_new_data_file_increments_counter() -> None:
    svc = MqttService(
        sensor_topic="s",
        command_topic="c",
        control_topic="x",
        on_sensor_message=lambda _: None,
    )

    file0 = svc._new_data_file()
    file1 = svc._new_data_file()

    assert file0.endswith("_data_0.csv")
    assert file1.endswith("_data_1.csv")
    assert file0 != file1
