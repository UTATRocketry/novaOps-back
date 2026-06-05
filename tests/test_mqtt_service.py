import json

from app.mqtt_service import MqttService


class _FakeInfo:
    rc = 0  # mqtt.MQTT_ERR_SUCCESS


class _FakeClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def publish(self, topic, payload, qos=1):
        self.published.append((topic, json.loads(payload)))
        return _FakeInfo()


def _connected_service() -> tuple[MqttService, _FakeClient]:
    svc = MqttService(
        sensor_topic="nova/telemetry",
        command_topic="nova/command",
        control_topic="nova/command",
        on_sensor_message=lambda _: None,
    )
    client = _FakeClient()
    svc._client = client
    svc._connected = True
    return svc, client


def test_new_data_file_increments_counter() -> None:
    svc = MqttService(
        sensor_topic="s",
        command_topic="c",
        control_topic="x",
        on_sensor_message=lambda _: None,
    )

    file0 = svc._new_data_file()
    file1 = svc._new_data_file()

    assert file0.endswith("_data_0")
    assert file1.endswith("_data_1")
    assert file0 != file1


def test_publish_data_saving_emits_start_command() -> None:
    svc, client = _connected_service()

    svc.publish_data_saving(True)

    assert len(client.published) == 1
    topic, payload = client.published[0]
    assert topic == "nova/command"
    assert payload["source"] == "novaOps"
    assert payload["command"]["type"] == "data_file"
    assert payload["command"]["action"] == "start_data_saving"
    assert "filename" in payload["command"]


def test_publish_data_saving_emits_stop_command() -> None:
    svc, client = _connected_service()

    svc.publish_data_saving(False)

    _, payload = client.published[0]
    assert payload["command"]["action"] == "stop_data_saving"
    assert "filename" not in payload["command"]


def test_publish_device_commands_wraps_fas_cmd() -> None:
    svc, client = _connected_service()

    svc.publish_device_commands(
        [{"type": "fas_cmd", "node": "FMC", "command": "SET_FLIGHT_STATE", "state": "ARMED"}]
    )

    assert len(client.published) == 1
    topic, payload = client.published[0]
    assert topic == "nova/command"
    assert payload == {
        "source": "novaOps",
        "command": {"type": "fas_cmd", "node": "FMC", "command": "SET_FLIGHT_STATE", "state": "ARMED"},
    }
