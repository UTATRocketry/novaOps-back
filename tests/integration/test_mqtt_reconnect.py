"""MqttService against a real broker that is killed and restarted."""
from __future__ import annotations

import json
import logging
import queue
import threading
import time

import paho.mqtt.client as mqtt
import pytest

from app.services.mqtt_service import MqttService

pytestmark = pytest.mark.integration

TIMEOUT_S = 20


def _wait_until(condition, what: str, timeout_s: float = TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"Timed out after {timeout_s}s waiting for: {what}")


def _client(broker, subscribe: str | None = None) -> tuple[mqtt.Client, queue.Queue]:
    received: queue.Queue = queue.Queue()
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.on_message = lambda _c, _u, msg: received.put((msg.topic, json.loads(msg.payload)))
    client.subscribed = threading.Event()  # type: ignore[attr-defined]
    client.on_subscribe = lambda *_: client.subscribed.set()  # type: ignore[attr-defined]
    if subscribe:
        client.on_connect = lambda c, *_: c.subscribe(subscribe)
    client.connect_async(broker.host, broker.port)
    client.loop_start()
    return client, received


@pytest.fixture
def service(broker_control, monkeypatch):
    monkeypatch.setenv("NOVA_MQTT_BROKER", broker_control.host)
    monkeypatch.setenv("NOVA_MQTT_PORT", str(broker_control.port))
    engine: list[dict] = []
    svc = MqttService("nova/telemetry/engine", "nova/command", "nova/command", on_sensor_message=engine.append)
    svc.engine = engine  # type: ignore[attr-defined]
    svc.start()
    try:
        _wait_until(lambda: svc._connected, "initial connect")
        yield svc
    finally:
        svc.stop()


def test_service_reconnects_resubscribes_and_publishes_again(service, broker_control, caplog) -> None:
    listener, commands = _client(broker_control, subscribe="nova/command")
    try:
        assert listener.subscribed.wait(TIMEOUT_S), "listener never subscribed"
        service.publish_device_commands([{"type": "relay", "id": 1, "state": 1}])
        topic, payload = commands.get(timeout=TIMEOUT_S)
        assert (topic, payload["command"]["id"]) == ("nova/command", 1)

        broker_control.stop()
        _wait_until(lambda: not service._connected, "service to notice the broker died")

        # Commands sent while the broker is down are dropped, not queued: a
        # valve command replayed minutes later would be dangerous.
        with caplog.at_level(logging.WARNING, logger="app.services.mqtt_service"):
            service.publish_device_commands([{"type": "relay", "id": 2, "state": 1}])
        assert "MQTT unavailable" in caplog.text

        listener.subscribed.clear()  # before the restart, so its resubscribe is not missed
        broker_control.start()
        _wait_until(lambda: service._connected, "service to reconnect")
        _wait_until(lambda: listener.subscribed.is_set(), "listener to resubscribe")

        # Telemetry flows again: proves the backend re-subscribed.
        sender, _ = _client(broker_control)
        try:
            _wait_until(lambda: sender.is_connected(), "sender to connect")
            sender.publish("nova/telemetry/engine", json.dumps({"source": "FAS", "sensors": []}), qos=1)
            _wait_until(lambda: service.engine, "telemetry after reconnect")
        finally:
            sender.loop_stop()
            sender.disconnect()

        # Commands go out again, and the one sent during the outage never does.
        service.publish_device_commands([{"type": "relay", "id": 3, "state": 1}])
        ids = []
        deadline = time.monotonic() + TIMEOUT_S
        while 3 not in ids and time.monotonic() < deadline:
            try:
                ids.append(commands.get(timeout=1)[1]["command"]["id"])
            except queue.Empty:
                pass
        assert 3 in ids
        assert 2 not in ids
    finally:
        listener.loop_stop()
        listener.disconnect()
