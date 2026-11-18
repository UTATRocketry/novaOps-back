"""nova/stats/<node>[/info] and nova/state/<node>: routing, backend state, and novaSim's mirror."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.context import AppContext
from app.routers import nodes
from app.services.mqtt_service import InvalidTopicFilter, MqttService

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import sim_node_stats  # noqa: E402


class _Msg:
    def __init__(self, topic: str, payload: bytes | dict) -> None:
        self.topic = topic
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()


class _Client:
    def __init__(self) -> None:
        self.subscribed: list[str] = []

    def subscribe(self, topic):
        self.subscribed.append(topic)

    def unsubscribe(self, topic):
        pass


def _service():
    stats, state, raw, sensors = [], [], [], []
    svc = MqttService(
        sensor_topic="nova/telemetry/engine",
        command_topic="nova/command",
        control_topic="nova/control",
        on_sensor_message=sensors.append,
        on_raw_message=lambda t, p: raw.append((t, p)),
        stats_topic="nova/stats/#",
        state_topic="nova/state/#",
        on_stats_message=lambda t, p: stats.append((t, p)),
        on_state_message=lambda t, p: state.append((t, p)),
    )
    svc._client = _Client()
    return svc, stats, state, raw, sensors


def test_stats_and_state_topics_are_subscribed_on_connect() -> None:
    svc, *_ = _service()
    client = _Client()
    svc._connect_success = lambda _rc: True
    svc._on_connect(client, None, None, 0, None)
    assert "nova/stats/#" in client.subscribed
    assert "nova/state/#" in client.subscribed


def test_stats_info_and_state_reach_their_handlers_only() -> None:
    svc, stats, state, raw, sensors = _service()
    svc._on_message(None, None, _Msg("nova/stats/novaGround", {"seq": 1}))
    svc._on_message(None, None, _Msg("nova/stats/novaGround/info", {"schema": 1}))
    svc._on_message(None, None, _Msg("nova/state/novaGround", {"relays": {"mask": 5}}))

    assert stats == [("nova/stats/novaGround", {"seq": 1}), ("nova/stats/novaGround/info", {"schema": 1})]
    assert state == [("nova/state/novaGround", {"relays": {"mask": 5}})]
    assert [t for t, _ in raw] == ["nova/stats/novaGround", "nova/stats/novaGround/info", "nova/state/novaGround"]
    assert sensors == []


def test_empty_payload_clears_and_bad_payload_is_dropped() -> None:
    svc, stats, state, raw, _ = _service()
    svc._on_message(None, None, _Msg("nova/state/novaGround", b""))
    svc._on_message(None, None, _Msg("nova/stats/novaGround", b"not json"))
    svc._on_message(None, None, _Msg("nova/stats/novaGround", b"[1, 2]"))

    assert state == [("nova/state/novaGround", None)]
    assert stats == []
    assert raw == []


def test_adhoc_subscription_may_not_overlap_stats_or_state() -> None:
    svc, *_ = _service()
    with pytest.raises(InvalidTopicFilter):
        svc.subscribe_topic("nova/stats/#")
    with pytest.raises(InvalidTopicFilter):
        svc.subscribe_topic("nova/state/+")
    assert svc.subscribe_topic("nova/other/#") is True


@pytest.fixture
def context(tmp_path) -> AppContext:
    config = tmp_path / "system.yaml"
    config.write_text("Sensors: []\nActuators: []\n", encoding="utf-8")
    return AppContext(config, tmp_path / "data")


def test_context_keeps_latest_stats_info_and_state_per_node(context) -> None:
    context._on_stats_message("nova/stats/novaGround", {"seq": 3})
    context._on_stats_message("nova/stats/novaGround/info", {"actuation": "stopped"})
    context._on_stats_message("nova/stats/novaThermo", {"seq": 9})
    context._on_state_message("nova/state/novaGround", {"relays": {"ok": True, "mask": 1}})
    context._on_stats_message("nova/stats/bad/extra/levels", {"x": 1})
    context._on_state_message("nova/state/novaGround/extra", {"x": 1})

    runtime = context.runtime
    assert runtime.node_stats == {"novaGround": {"seq": 3}, "novaThermo": {"seq": 9}}
    assert runtime.node_stats_info == {"novaGround": {"actuation": "stopped"}}
    assert runtime.node_state == {"novaGround": {"relays": {"ok": True, "mask": 1}}}

    context._on_state_message("nova/state/novaGround", None)
    context._on_stats_message("nova/stats/novaThermo", None)
    assert runtime.node_state == {}
    assert "novaThermo" not in runtime.node_stats
    assert "novaThermo" not in context._stats_last_seen


def test_nodes_router_reports_what_was_received(context) -> None:
    context._on_stats_message("nova/stats/novaSim", {"seq": 1})
    context._on_state_message("nova/state/novaSim", {"schema": 1})

    app = FastAPI()
    app.include_router(nodes.router)
    from app.deps import get_context
    app.dependency_overrides[get_context] = lambda: context
    client = TestClient(app)

    body = client.get("/api/nodes").json()
    assert body == {"nodes": {"novaSim": {"info": None, "stats": {"seq": 1}, "state": {"schema": 1}}}}
    assert client.get("/api/nodes/novaSim").status_code == 200
    assert client.get("/api/nodes/nope").status_code == 404


# --- novaSim's mirror ---------------------------------------------------------

def test_sim_info_and_actuation_flag() -> None:
    stats = sim_node_stats.NodeStats(enabled=True, interval_ms=250, stop_actuate=True,
                                     loops={"publisher": 50.0})
    info = stats.info()
    assert info["schema"] == 1 and info["actuation"] == "stopped"
    assert info["stats_interval_ms"] == 250
    assert info["loops"] == {"publisher": {"configured_ms": 50.0}}
    assert stats.info_topic == "nova/stats/novaSim/info"
    assert stats.state_topic == "nova/state/novaSim"


def test_sim_stats_counts_overruns_and_commands_and_windows() -> None:
    stats = sim_node_stats.NodeStats(enabled=True, loops={"publisher": 50.0})
    for period in (50.0, 55.0, 120.0):
        stats.record_tick("publisher", period)
    stats.record_command("relay", 0.1, 0.4)

    first = stats.stats()
    loop = first["loops"]["publisher"]
    assert first["seq"] == 1
    assert loop["ticks"] == 3 and loop["overruns"] == 1
    assert loop["period_ms"]["last"]["count"] == 3
    assert loop["period_ms"]["last"]["max"] == 120.0
    assert first["commands"]["relay"]["count"] == 1
    assert first["commands"]["relay"]["received_to_done_ms"]["total"]["count"] == 1

    second = stats.stats()
    assert second["seq"] == 2
    assert second["loops"]["publisher"]["period_ms"]["last"]["count"] == 0
    assert second["loops"]["publisher"]["period_ms"]["total"]["count"] == 3


def test_sim_percentiles_stay_within_the_documented_error() -> None:
    dist = sim_node_stats.Distribution()
    for value in range(1, 1001):
        dist.add(float(value))
    summary = dist.summary()
    assert summary["min"] == 1.0 and summary["max"] == 1000.0
    assert 500 <= summary["p50"] <= 500 * 1.05
    assert 990 <= summary["p99"] <= 1000


def test_sim_relay_servo_gpio_state_follows_commands() -> None:
    stats = sim_node_stats.NodeStats(enabled=True)
    assert stats.state()["relays"]["mask"] == 0xFFFF
    stats.apply_relay(2, 0)
    stats.apply_servo(1, 1498)
    stats.apply_servo(3, "on")
    stats.apply_gpio(17, 1)

    state = stats.state()
    assert state["relays"] == {"ok": True, "mask": 0xFFFF & ~(1 << 2)}
    assert len(state["servos"]["channels"]) == 16
    assert state["servos"]["channels"][1] == {"id": 1, "output": "pulse", "pulse_us": 1498}
    assert state["servos"]["channels"][3] == {"id": 3, "output": "on"}
    assert state["servos"]["channels"][0] == {"id": 0, "output": "off"}
    assert state["gpios"]["outputs"] == [{"pin_id": 17, "state": 1}]


def test_sim_stop_actuate_drops_writes_and_counts_them() -> None:
    stats = sim_node_stats.NodeStats(enabled=True, stop_actuate=True)
    before = stats.state()
    stats.apply_relay(2, 0)
    stats.apply_servo(1, 1498)
    stats.apply_gpio(17, 1)

    after = stats.state()
    assert after["relays"] == before["relays"]
    assert after["servos"] == before["servos"]
    assert after["gpios"] == before["gpios"]
    assert stats.stats()["stopped_writes"] == 3


def test_sim_flag_parsing_matches_novaground() -> None:
    for text in ("on", "ON", "1", "true", "Yes"):
        assert sim_node_stats.parse_stats_flag(text) is True
    for text in ("off", "0", "false", "no"):
        assert sim_node_stats.parse_stats_flag(text) is False
    assert sim_node_stats.parse_stats_flag("maybe") is None
    assert sim_node_stats.parse_stats_ms("250") == 250
    for bad in ("0", "1.5", "", "-3", "abc"):
        assert sim_node_stats.parse_stats_ms(bad) is None
