"""novaSim's stand-in for novaGround's per-node config, status and trips."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import novaSim  # noqa: E402

from app.models import SystemConfig  # noqa: E402
from app.services import pi_config  # noqa: E402


class _Client:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict, bool]] = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, json.loads(payload), retain))


class _Msg:
    def __init__(self, topic: str, payload, retain: bool = False) -> None:
        self.topic = topic
        self.payload = json.dumps(payload).encode()
        self.retain = retain


@pytest.fixture(autouse=True)
def fresh_sim(monkeypatch):
    state = {
        "configured": False, "config_hash": None, "applied_ms": None, "rejected": [],
        "thermocouples": {}, "limits": {}, "inputs": [], "levels": {}, "left_safe": {},
        "tripped": [], "timeout_ms": None, "last_beat": None, "will_seen": False,
    }
    monkeypatch.setattr(novaSim, "_pi", state)
    return state


def _backend_message() -> dict:
    config = SystemConfig.model_validate(yaml.safe_load("""
Sensors:
  - {name: DOOR, type: DI, binding: {source: GCS, gpio_channel: 5}}
  - {name: TC, type: TC, binding: {source: GCS, hat_id: 1, channel_id: 0}, tc_type: T}
Actuators:
  - name: HTR
    type: gpio_device
    binding: {target: GCS, gpio_channel: 17}
    actions: {max_on_s: 600, on_comms_loss: safe}
  - name: RLY
    type: powered_device
    binding: {target: GCS, relay_channel: 2}
    actions: {max_on_s: 30, on_comms_loss: safe}
"""))
    return pi_config.build_node_config(config, "GCS", "abcd")


def test_backend_config_is_applied_cleanly() -> None:
    status = novaSim.apply_pi_config(_backend_message())
    assert status["config_hash"] == "abcd" and status["rejected"] == []
    assert status["thermocouples"] == {"1": {"0": "T"}}
    assert status["reserved_pins"] == [0, 1, 2, 3, 7, 8, 9, 10, 11, 12, 13, 16, 20, 21, 26]
    assert novaSim.pi_gpio_readings()[-1] == {"pin_id": 17, "state": 0}


def test_bad_entries_are_rejected() -> None:
    status = novaSim.apply_pi_config({
        "config_hash": "x",
        "gpio": {"outputs": {"8": {"max_on_ms": None, "on_comms_loss": "hold"},
                             "30": {"max_on_ms": None, "on_comms_loss": "hold"},
                             "17": {"max_on_ms": None, "on_comms_loss": "hold"}},
                 "inputs": [17]},
        "daq": {"0": {"0": "Q"}},
        "bogus": 1,
    })
    items = {r["item"]: r["reason"] for r in status["rejected"]}
    assert items["gpio.outputs.8"] == "reserved (SPI0 CE0)"
    assert items["gpio.outputs.30"] == "outside BCM 0-27"
    assert items["gpio.inputs.17"] == "pin is also an output"
    assert "daq.0.0" in items and items["bogus"] == "unknown key"


def test_config_message_gets_a_retained_status() -> None:
    client = _Client()
    novaSim.on_message(client, None, _Msg("nova/config/novaGround", _backend_message(), retain=True))
    [(topic, status, retained)] = client.published
    assert topic == "nova/config/novaGround/status" and retained and status["config_hash"] == "abcd"


def test_actuator_commands_only_on_the_nodes_topic(monkeypatch) -> None:
    novaSim.apply_pi_config(_backend_message())
    novaSim.on_message(_Client(), None, _Msg("nova/heartbeat/backend", {"online": True, "seq": 0, "time_ms": 0}))
    seen: list = []
    monkeypatch.setattr(novaSim.STATS, "apply_gpio", lambda pin, state: seen.append((pin, state)))
    command = {"source": "novaOps", "command": {"type": "gpio", "id": 17, "state": 1}}
    novaSim.on_message(_Client(), None, _Msg("nova/command", command))
    assert seen == []
    novaSim.on_message(_Client(), None, _Msg("nova/command/novaGround", command))
    assert (17, 1) in seen
    unconfigured = {"source": "novaOps", "command": {"type": "gpio", "id": 22, "state": 1}}
    assert novaSim.pi_command_allowed(unconfigured["command"]) == "not a configured GPIO output"


def test_comms_loss_trips_safe_outputs_and_a_command_clears_it() -> None:
    novaSim.apply_pi_config(_backend_message())
    novaSim.pi_on_heartbeat({"online": True, "seq": 1, "time_ms": 1}, retained=False)
    assert novaSim.pi_command_allowed({"type": "gpio", "id": 17, "state": 1}) is None

    novaSim.pi_on_heartbeat({"online": False}, retained=True)  # the backend's last will
    client = _Client()
    novaSim.pi_supervise(client)
    [(_, status, _)] = client.published
    assert status["tripped"][0]["output"] == "gpio:17" and status["tripped"][0]["reason"] == "comms_loss"
    # While comms are lost, moving a safe output off its safe level is refused.
    assert novaSim.pi_command_allowed({"type": "gpio", "id": 17, "state": 1}) == "heartbeat stale"

    novaSim.pi_on_heartbeat({"online": True, "seq": 2, "time_ms": 2}, retained=False)
    assert novaSim.pi_command_allowed({"type": "gpio", "id": 17, "state": 0}) is None
    client = _Client()
    novaSim.pi_supervise(client)
    assert client.published[0][1]["tripped"] == []


def test_retained_online_is_not_a_beat() -> None:
    novaSim.apply_pi_config(_backend_message())
    novaSim.pi_on_heartbeat({"online": True}, retained=True)
    assert novaSim._pi_heartbeat_stale()


def test_backend_status_view_accepts_the_sims_status() -> None:
    message = _backend_message()
    status = novaSim.apply_pi_config(message)
    config = SystemConfig()
    view = pi_config.status_view("novaGround", status, config, "abcd")
    assert view["mismatch"] is False and view["received"] is True
