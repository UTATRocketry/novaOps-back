"""MqttService with hostile input and without a broker."""
import json
import logging
from types import SimpleNamespace

import pytest

from app.context import AppContext
from app.services.mqtt_service import MqttService

CONFIG = """
Sensors:
  - name: PFT
    type: PT
    unit: psi
    binding: {source: FAS, node: EPB_1, channel: 1}
    convert: {method: none}
Actuators: []
"""

TOPICS = [
    "nova/telemetry/engine",
    "nova/telemetry/flight",
    "nova/console",
    "nova/control",
    "some/other/topic",
]

BAD_BODIES = [
    b"",
    b"not json",
    b"\xff\xfe\x00garbage",       # not UTF-8
    b"[]",
    b"null",
    b'"text"',
    b"123",
    b'{"sensors": 5}',
    b'{"sensors": [1, 2]}',
    b'{"sensors": [{"node": 7, "channel": "x", "value": "y"}]}',
    b'{"data": "x", "events": 4}',
    b'{"state": null}',
    b'{"line": ["a"]}',
]


@pytest.fixture
def ctx(tmp_path):
    path = tmp_path / "system.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    ctx = AppContext(path, tmp_path / "data")
    ctx._broadcast_from_mqtt = lambda payload: None  # type: ignore[method-assign]
    return ctx


@pytest.mark.parametrize("body", BAD_BODIES, ids=lambda b: repr(b)[:30])
@pytest.mark.parametrize("topic", TOPICS)
def test_malformed_payload_never_escapes_the_callback(ctx, topic, body) -> None:
    ctx.mqtt_service._on_message(None, None, SimpleNamespace(topic=topic, payload=body))

    # The pipeline still works afterwards.
    good = {"source": "FAS", "sensors": [{"node": "EPB_1", "channel": 1, "value": 2.0, "timestamp": 1}]}
    ctx.mqtt_service._on_message(None, None, SimpleNamespace(topic="nova/telemetry/engine", payload=json.dumps(good).encode()))
    assert ctx.runtime.latest_engine_data[0]["value"] == 2.0


def test_failure_is_logged(ctx, caplog) -> None:
    with caplog.at_level(logging.ERROR, logger="app.services.mqtt_service"):
        ctx.mqtt_service._on_message(None, None, SimpleNamespace(topic="nova/console", payload=b"not json"))
    assert "Failed to process MQTT message" in caplog.text


# --- no broker ---

def _service() -> MqttService:
    return MqttService("s", "nova/command", "nova/control", on_sensor_message=lambda _: None)


@pytest.mark.parametrize(
    "publish",
    [
        lambda svc: svc.publish_device_commands([{"type": "relay", "id": 1, "state": 1}]),
        lambda svc: svc.publish_console({"x": 1}),
        lambda svc: svc.publish_console_command({"action": "start"}),
        lambda svc: svc.publish_data_saving(True),
    ],
    ids=["device", "console", "console_command", "data_saving"],
)
@pytest.mark.parametrize("state", ["no_client", "disconnected"])
def test_publish_while_disconnected_is_dropped_with_a_warning(publish, state, caplog) -> None:
    svc = _service()
    sent: list = []
    if state == "disconnected":
        svc._client = SimpleNamespace(publish=lambda *a, **k: sent.append(a))
        svc._connected = False

    with caplog.at_level(logging.WARNING, logger="app.services.mqtt_service"):
        publish(svc)

    assert sent == []
    assert "MQTT unavailable" in caplog.text


def test_failed_publish_is_logged_not_raised(caplog) -> None:
    svc = _service()
    svc._connected = True
    svc._client = SimpleNamespace(publish=lambda *a, **k: SimpleNamespace(rc=4))

    with caplog.at_level(logging.ERROR, logger="app.services.mqtt_service"):
        svc.publish_device_commands([{"type": "relay", "id": 1, "state": 1}])

    assert "publish failed" in caplog.text


# --- connection state ---

class _Subscriber:
    def __init__(self) -> None:
        self.topics: list[str] = []

    def subscribe(self, topic: str) -> None:
        self.topics.append(topic)


def test_on_connect_subscribes_to_every_topic_once() -> None:
    svc = MqttService(
        "nova/telemetry/engine", "nova/command", "nova/control",
        on_sensor_message=lambda _: None,
        flight_topic="nova/telemetry/flight",
        console_topic="nova/console",
        on_control_message=lambda _: None,
    )
    client = _Subscriber()

    svc._on_connect(client, None, None, 0, None)

    assert svc._connected
    assert client.topics == ["nova/telemetry/engine", "nova/telemetry/flight", "nova/console", "nova/control"]


def test_control_topic_is_skipped_without_a_control_handler() -> None:
    svc = MqttService("s", "c", "ctl", on_sensor_message=lambda _: None)
    client = _Subscriber()
    svc._on_connect(client, None, None, 0, None)
    assert client.topics == ["s"]


@pytest.mark.parametrize("reason", [5, SimpleNamespace(is_failure=True), SimpleNamespace(value=135), object()])
def test_refused_connection_stays_disconnected(reason) -> None:
    svc = _service()
    client = _Subscriber()
    svc._on_connect(client, None, None, reason, None)
    assert not svc._connected
    assert client.topics == []


def test_disconnect_callback_marks_the_service_unavailable() -> None:
    svc = _service()
    svc._connected = True
    svc._on_disconnect(None, None, None, 7, None)
    assert not svc._connected


def test_stop_without_start_is_harmless() -> None:
    _service().stop()
