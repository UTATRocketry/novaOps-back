"""Backend heartbeat and MQTT last will (docs/design/pi-io-config.md, "Heartbeat")."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.context import AppContext
from app.services import mqtt_service as mqtt_module
from app.services.mqtt_service import MqttService

TOPIC = "nova/heartbeat/backend"


class _Info:
    rc = 0

    def __init__(self) -> None:
        self.waited = False

    def wait_for_publish(self, _timeout=None):
        self.waited = True
        return True


class _Client:
    def __init__(self, *args, **kwargs) -> None:
        self.published: list[tuple[str, dict, int, bool]] = []
        self.will = None
        self.calls: list[str] = []

    def publish(self, topic, payload, qos=1, retain=False):
        self.published.append((topic, json.loads(payload), qos, retain))
        return _Info()

    def subscribe(self, topic):
        pass

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, json.loads(payload), qos, retain)
        self.calls.append("will_set")

    def connect_async(self, *_args, **_kwargs):
        self.calls.append("connect_async")

    def loop_start(self):
        self.calls.append("loop_start")

    def loop_stop(self):
        self.calls.append("loop_stop")

    def disconnect(self):
        self.calls.append("disconnect")


def _service(**kwargs) -> MqttService:
    return MqttService("nova/telemetry/engine", "nova/command", "nova/control",
                       on_sensor_message=lambda _: None, heartbeat_topic=TOPIC, **kwargs)


def test_last_will_is_retained_offline(monkeypatch) -> None:
    clients: list[_Client] = []

    def make_client(*args, **kwargs):
        clients.append(_Client())
        return clients[-1]

    monkeypatch.setattr(mqtt_module.mqtt, "Client", make_client)
    svc = _service()
    svc.start()
    assert clients[0].will == (TOPIC, {"online": False}, 1, True)
    # The will must be set before connecting.
    assert clients[0].calls[:2] == ["will_set", "connect_async"]


def test_connect_publishes_retained_online() -> None:
    svc = _service()
    client = _Client()
    svc._client = client
    svc._on_connect(client, None, None, 0, None)
    assert (TOPIC, {"online": True}, 1, True) in client.published


def test_no_heartbeat_topic_means_no_heartbeat_traffic() -> None:
    svc = MqttService("s", "c", "x", on_sensor_message=lambda _: None)
    client = _Client()
    svc._client = client
    svc._on_connect(client, None, None, 0, None)
    assert client.published == []
    assert svc.publish_heartbeat(1, 2) is False


def test_beat_is_not_retained_and_carries_seq_and_time() -> None:
    svc = _service()
    client = _Client()
    svc._client, svc._connected = client, True
    assert svc.publish_heartbeat(7, 1795110000000) is True
    assert client.published == [(TOPIC, {"online": True, "seq": 7, "time_ms": 1795110000000}, 0, False)]


def test_no_beat_while_disconnected() -> None:
    svc = _service()
    svc._client = _Client()
    assert svc.publish_heartbeat(1, 2) is False
    assert svc._client.published == []


def test_clean_stop_publishes_offline_before_disconnecting() -> None:
    svc = _service()
    client = _Client()
    svc._client, svc._connected = client, True
    svc.stop()
    assert client.published == [(TOPIC, {"online": False}, 1, True)]
    assert client.calls == ["loop_stop", "disconnect"]


def test_heartbeat_loop_runs_on_the_event_loop_every_250_ms(tmp_path, monkeypatch) -> None:
    path = tmp_path / "system.yaml"
    path.write_text("Sensors: []\n", encoding="utf-8")
    ctx = AppContext(path, tmp_path / "data")
    beats: list[tuple[int, int]] = []

    def publish(seq, time_ms):
        beats.append((seq, time_ms))
        return True

    ctx.mqtt_service.publish_heartbeat = publish  # type: ignore[method-assign]
    monkeypatch.setattr("app.services.pi_config.HEARTBEAT_INTERVAL_S", 0.01)

    async def run() -> None:
        task = asyncio.create_task(ctx._heartbeat_loop())
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert len(beats) >= 3
    assert [seq for seq, _ in beats] == list(range(len(beats)))
    assert all(t > 1_600_000_000_000 for _, t in beats)


def test_default_interval_is_250_ms() -> None:
    from app.services import pi_config

    assert pi_config.HEARTBEAT_INTERVAL_S == pytest.approx(0.25)
    assert pi_config.HEARTBEAT_TOPIC == TOPIC
