"""The Pi config status view's apply state and times, and the node info
(with novaGround's hardware report) as the Devices page gets it."""
from __future__ import annotations

import json

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.context import AppContext
from app.models import SystemConfig
from app.routers import nodes as nodes_router
from app.services import pi_config

CONFIG = """
Actuators:
  - name: TANK-HTR-PWR
    type: gpio_device
    binding: {target: TCS, gpio_channel: 17}
    actions: {gpio_commands: [ARM, DISARM], max_on_s: 1800, on_comms_loss: safe}
"""


def _config() -> SystemConfig:
    return SystemConfig.model_validate(yaml.safe_load(CONFIG))


def _status(**fields) -> dict:
    status = {"config_hash": "h1", "applied_ms": 10, "rejected": [], "reserved_pins": [0, 1],
              "thermocouples": {}, "tripped": []}
    status.update(fields)
    return status


@pytest.mark.parametrize(
    ("status", "expected", "state"),
    [
        (None, "h1", "waiting"),
        (_status(), "h1", "applied"),
        (_status(), "h2", "pending"),
        (_status(rejected=[{"item": "gpio.outputs.8", "reason": "reserved"}]), "h1", "rejected"),
        # Rejected outranks pending: the Pi has answered, and refused something.
        (_status(config_hash="h0", rejected=[{"item": "x", "reason": "y"}]), "h1", "rejected"),
    ],
)
def test_apply_state(status, expected, state) -> None:
    assert pi_config.status_view("novaThermo", status, _config(), expected)["apply_state"] == state


def test_times_are_passed_through_and_none_by_default() -> None:
    view = pi_config.status_view("novaThermo", _status(), _config(), "h1")
    assert view["received_ms"] is None and view["published_ms"] is None
    view = pi_config.status_view("novaThermo", _status(), _config(), "h1", received_ms=50, published_ms=40)
    assert view["received_ms"] == 50 and view["published_ms"] == 40
    # No status: nothing was received, whatever the caller passes.
    assert pi_config.status_view("novaThermo", None, _config(), "h1", received_ms=50)["received_ms"] is None


class _Info:
    rc = 0


class _Client:
    def __init__(self) -> None:
        self.published: list[str] = []

    def publish(self, topic, payload, qos=1, retain=False):
        self.published.append(topic)
        return _Info()

    def subscribe(self, topic):
        pass


@pytest.fixture
def context(tmp_path):
    path = tmp_path / "system.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    ctx = AppContext(path, tmp_path / "data")
    ctx.runtime.initialize_all(ctx.config_service.config)
    ctx.mqtt_service._client = _Client()
    ctx.mqtt_service._connected = True
    return ctx


def test_context_records_received_and_published_times(context, monkeypatch) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr("app.context.time.time", lambda: clock["t"])

    context.publish_node_configs()
    assert context.pi_status_views()["novaThermo"]["published_ms"] == 1_000_000
    # A republish of the same config (e.g. a reconnect) keeps the first time.
    clock["t"] = 2000.0
    context.publish_node_configs()
    assert context.pi_status_views()["novaThermo"]["published_ms"] == 1_000_000

    messages = context.apply_config_status("novaThermo", _status(config_hash=context.config_service.active_hash))
    view = messages[0]["status"]
    assert view["apply_state"] == "applied" and view["received_ms"] == 2_000_000
    assert view["published_ms"] == 1_000_000

    # The status being cleared drops its time.
    context.apply_config_status("novaThermo", None)
    assert context.pi_status_views()["novaThermo"]["received_ms"] is None


def test_published_time_is_not_set_when_mqtt_is_down(context) -> None:
    context.mqtt_service._connected = False
    context.mqtt_service._client = None
    context.publish_node_configs()
    assert context.pi_status_views()["novaGround"]["published_ms"] is None


HARDWARE = {
    "build_id": "novaThermo-1.4.0",
    "parts": {
        "gpio": {"expected": True, "found": True},
        "daq": {"expected": True, "found": False},
        "relays": {"expected": False, "found": False},
        "servo": {"expected": False, "found": True},
    },
    "missing": ["daq"],
}


def test_info_with_a_hardware_report_reaches_rest_and_websocket_unchanged(context) -> None:
    sent: list[dict] = []
    context._broadcast_from_mqtt = sent.append  # type: ignore[method-assign]
    info = {"schema": 1, "build_id": "novaThermo-1.4.0", "started_ms": 5, "stats_interval_ms": 1000,
            "actuation": "enabled", "hardware": HARDWARE, "loops": {"publisher": {"configured_ms": 50.0}}}

    class _Msg:
        topic = "nova/stats/novaThermo/info"
        payload = json.dumps(info).encode()

    context.mqtt_service._on_message(None, None, _Msg())
    assert {"type": "node_stats_info", "node": "novaThermo", "info": info} in sent

    app = FastAPI()
    app.state.ctx = context
    app.include_router(nodes_router.router)
    body = TestClient(app).get("/api/nodes/novaThermo").json()
    assert body["info"]["hardware"] == HARDWARE
