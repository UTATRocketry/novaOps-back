"""nova/config/<node>, its status, trips, and how they reach the frontend."""
from __future__ import annotations

import json

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.context import AppContext
from app.models import SystemConfig
from app.routers import config as config_router
from app.services import pi_config

# The design doc's test-config additions, plus a few extras for coverage.
CONFIG = """
Sensors:
  - name: TC_A
    type: TC
    unit: C
    binding: {source: TCS, hat_id: 1, channel_id: 1}
    tc_type: T
  - name: TC_B
    type: TC
    unit: C
    binding: {source: TCS, hat_id: 1, channel_id: 0}
  - name: TC_GCS
    type: TC
    binding: {source: GCS, hat_id: 2, channel_id: 3}
    tc_type: J
  - name: PT_TCS
    type: PT
    binding: {source: TCS, hat_id: 0, channel_id: 0}
  - name: TC_FAS
    type: TC
    binding: {source: FAS, node: EPB_0, channel: 2}
  - name: DOOR
    type: DI
    binding: {source: TCS, gpio_channel: 5}
Actuators:
  - name: TANK-HTR-PWR
    type: gpio_device
    binding: {target: TCS, gpio_channel: 17}
    actions: {gpio_commands: [ARM, DISARM], max_on_s: 1800, on_comms_loss: safe}
  - name: TANK-HTR-HEAT
    type: gpio_device
    binding: {target: TCS, gpio_channel: 27}
    actions: {gpio_commands: [ARM, DISARM], max_on_s: 600, on_comms_loss: safe}
  - name: Test_Sol
    type: solenoid
    binding: {target: GCS, relay_channel: 0}
    actions: {relay_type: nominally_off, solenoid_type: nominally_closed}
  - name: Test_CommsLoss_Relay
    type: powered_device
    binding: {target: GCS, relay_channel: 2}
    actions: {relay_type: nominally_off, max_on_s: 30, on_comms_loss: safe}
  - name: NO_LIMIT_GPIO
    type: gpio_device
    binding: {target: GCS, gpio_channel: 22}
  - name: SAFE_SOL
    type: solenoid
    binding: {target: TCS, relay_channel: 3}
    actions: {relay_type: nominally_on, solenoid_type: nominally_closed, on_comms_loss: safe}
  - name: PUMP
    type: motor
    binding: {target: TCS, relay_channel: 6, reverse_relay_channel: 7}
    actions: {reversible: true, invert_relays: true, on_comms_loss: safe}
Heartbeat:
  timeout_ms: 2000
"""


def _config() -> SystemConfig:
    return SystemConfig.model_validate(yaml.safe_load(CONFIG))


# --- the config message ---

def test_novathermo_config_message_matches_the_design() -> None:
    assert pi_config.build_node_config(_config(), "TCS", "3f9a1c0be27d4a55") == {
        "config_hash": "3f9a1c0be27d4a55",
        "gpio": {
            "outputs": {
                "17": {"max_on_ms": 1800000, "on_comms_loss": "safe"},
                "27": {"max_on_ms": 600000, "on_comms_loss": "safe"},
            },
            "inputs": [5],
        },
        "relays": {
            "3": {"max_on_ms": None, "on_comms_loss": "safe"},
            "6": {"max_on_ms": None, "on_comms_loss": "safe"},
            "7": {"max_on_ms": None, "on_comms_loss": "safe"},
        },
        "daq": {"1": {"0": "K", "1": "T"}},
        "heartbeat": {"timeout_ms": 2000},
    }


def test_novaground_config_message() -> None:
    message = pi_config.build_node_config(_config(), "GCS", "abc")
    assert message == {
        "config_hash": "abc",
        # An output with no limits is still listed: the Pi claims every configured output.
        "gpio": {"outputs": {"22": {"max_on_ms": None, "on_comms_loss": "hold"}}, "inputs": []},
        # Relays without limits (Test_Sol) are left out.
        "relays": {"2": {"max_on_ms": 30000, "on_comms_loss": "safe"}},
        "daq": {"2": {"3": "J"}},
        "heartbeat": {"timeout_ms": 2000},
    }
    # Keys in the order the design shows.
    assert list(message) == ["config_hash", "gpio", "relays", "daq", "heartbeat"]


def test_shared_output_limits_are_merged() -> None:
    config = SystemConfig.model_validate({"Actuators": [
        {"name": "A", "type": "gpio_device", "binding": {"target": "TCS", "gpio_channel": 17},
         "actions": {"max_on_s": 60}},
        {"name": "B", "type": "powered_gpio_device", "binding": {"target": "TCS", "gpio_channel": 17, "relay_channel": 1},
         "actions": {"max_on_s": 30, "on_comms_loss": "safe"}},
    ]})
    message = pi_config.build_node_config(config, "TCS", None)
    assert message["gpio"]["outputs"] == {"17": {"max_on_ms": 30000, "on_comms_loss": "safe"}}
    assert message["relays"] == {"1": {"max_on_ms": 30000, "on_comms_loss": "safe"}}


def test_empty_config_still_gets_a_message_per_pi() -> None:
    messages = pi_config.build_all_node_configs(SystemConfig(), "h")
    assert set(messages) == {"novaGround", "novaThermo"}
    assert messages["novaThermo"] == {
        "config_hash": "h", "gpio": {"outputs": {}, "inputs": []}, "relays": {}, "daq": {},
        "heartbeat": {"timeout_ms": 2000},
    }


# --- status view ---

def _status(**fields) -> dict:
    status = {"config_hash": "h1", "applied_ms": 1, "rejected": [], "reserved_pins": [0, 1],
              "thermocouples": {"1": {"0": "K"}}, "tripped": []}
    status.update(fields)
    return status


def test_status_view_matches() -> None:
    view = pi_config.status_view("novaThermo", _status(), _config(), "h1")
    assert view["mismatch"] is False and view["mismatch_reasons"] == []
    assert view["received"] is True and view["target"] == "TCS"
    assert view["thermocouples"] == {"1": {"0": "K"}} and view["reserved_pins"] == [0, 1]


def test_status_view_mismatch_on_hash_or_rejected() -> None:
    assert pi_config.status_view("novaThermo", _status(), _config(), "h2")["mismatch_reasons"] == ["config_hash"]
    rejected = [{"item": "gpio.outputs.8", "reason": "reserved (SPI0 CE0)"}]
    view = pi_config.status_view("novaThermo", _status(rejected=rejected), _config(), "h1")
    assert view["mismatch"] and view["mismatch_reasons"] == ["rejected"]
    assert view["rejected"] == rejected


def test_status_view_before_any_status() -> None:
    view = pi_config.status_view("novaGround", None, _config(), "h1")
    assert view["received"] is False and view["mismatch"] is False
    assert view["config_hash"] is None and view["tripped"] == [] and view["rejected"] == []


def test_status_view_names_tripped_actuators() -> None:
    trips = [{"output": "gpio:17", "reason": "comms_loss", "time_ms": 5},
             {"output": "relay:9", "reason": "max_on_time", "time_ms": 6}]
    view = pi_config.status_view("novaThermo", _status(tripped=trips), _config(), "h1")
    assert view["tripped"] == [
        {"output": "gpio:17", "reason": "comms_loss", "time_ms": 5, "actuators": ["TANK-HTR-PWR"]},
        {"output": "relay:9", "reason": "max_on_time", "time_ms": 6, "actuators": []},
    ]


# --- trips applied to actuator state ---

@pytest.fixture
def ctx(tmp_path):
    path = tmp_path / "system.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    context = AppContext(path, tmp_path / "data")
    context.runtime.initialize_all(context.config_service.config)
    published: list = []
    context.mqtt_service.publish_device_commands = lambda cmds, topic=None: published.append((topic, cmds))  # type: ignore[method-assign]
    return context, published


def _trip(output: str, reason: str = "comms_loss", time_ms: int = 100) -> dict:
    return {"output": output, "reason": reason, "time_ms": time_ms}


@pytest.mark.parametrize(
    ("node", "output", "name", "before", "after"),
    [
        # GPIO safe level 0 -> disarmed.
        ("novaThermo", "gpio:17", "TANK-HTR-PWR", {"arming": "armed"}, {"arming": "disarmed"}),
        # nominally_off relay de-energized -> off.
        ("novaGround", "relay:2", "Test_CommsLoss_Relay", {"power": "on"}, {"power": "off"}),
        # nominally_on relay de-energized -> powered; nominally_closed valve powered -> open.
        ("novaThermo", "relay:3", "SAFE_SOL", {"position": "closed"}, {"position": "open"}),
    ],
)
def test_trip_sets_safe_state_with_the_command_rules(ctx, node, output, name, before, after) -> None:
    context, published = ctx
    context.runtime.actuator_states[name].update(before)

    messages = context.apply_config_status(node, _status(tripped=[_trip(output)]))

    state = context.runtime.actuator_states[name]
    for key, value in after.items():
        assert state[key] == value
    assert state["tripped"] == "comms_loss" and state["tripped_output"] == output and state["tripped_ms"] == 100
    assert published == []  # never re-sends a command
    assert [m["type"] for m in messages] == ["pi_config_status", "actuator_states"]


def test_motor_trip_uses_its_relay_pattern(ctx) -> None:
    context, _ = ctx
    context.runtime.actuator_states["PUMP"]["motion"] = "forward"
    # invert_relays: forward drives relay 6 low (wire 0). De-energized (1) on 6
    # with relay 7 still at its "forward" level (1) is "stop".
    context.apply_config_status("novaThermo", _status(tripped=[_trip("relay:6")]))
    assert context.runtime.actuator_states["PUMP"]["motion"] == "stop"


def test_redelivered_trip_is_not_applied_again_and_clears_when_gone(ctx) -> None:
    context, _ = ctx
    status = _status(tripped=[_trip("gpio:27", "max_on_time")])
    context.apply_config_status("novaThermo", status)
    assert context.runtime.actuator_states["TANK-HTR-HEAT"]["arming"] == "disarmed"

    # An operator re-arms; the same retained status arrives again.
    context.runtime.actuator_states["TANK-HTR-HEAT"]["arming"] = "armed"
    context.apply_config_status("novaThermo", status)
    assert context.runtime.actuator_states["TANK-HTR-HEAT"]["arming"] == "armed"

    # The Pi clears the trip (next command) and republishes.
    messages = context.apply_config_status("novaThermo", _status())
    state = context.runtime.actuator_states["TANK-HTR-HEAT"]
    assert "tripped" not in state and "tripped_output" not in state and "tripped_ms" not in state
    assert "actuator_states" in [m["type"] for m in messages]


def test_trips_on_one_pi_do_not_touch_the_other(ctx) -> None:
    context, _ = ctx
    # gpio:17 on novaGround is not TANK-HTR-PWR (which is on novaThermo).
    context.runtime.actuator_states["TANK-HTR-PWR"]["arming"] = "armed"
    context.apply_config_status("novaGround", _status(tripped=[_trip("gpio:17")]))
    assert context.runtime.actuator_states["TANK-HTR-PWR"]["arming"] == "armed"


def test_status_from_mqtt_is_stored_without_an_event_loop(ctx) -> None:
    context, _ = ctx
    context._on_config_status_message("nova/config/novaThermo/status", _status())
    context._on_config_status_message("nova/config/novaMock/status", _status())  # not a Pi target
    context._on_config_status_message("nova/config/novaThermo", _status())  # not a status topic
    assert set(context.runtime.pi_config_status) == {"novaThermo"}
    context._on_config_status_message("nova/config/novaThermo/status", None)  # retained message cleared
    assert context.runtime.pi_config_status == {}


def test_mqtt_service_routes_the_status_topic(ctx) -> None:
    context, _ = ctx

    class _Msg:
        topic = "nova/config/novaGround/status"
        payload = json.dumps(_status()).encode()

    assert "nova/config/+/status" in context.mqtt_service._core_topics()
    context.mqtt_service._on_message(None, None, _Msg())
    assert context.runtime.pi_config_status["novaGround"]["config_hash"] == "h1"


# --- publishing the config, on connect and on every change ---

class _Info:
    rc = 0


class _Client:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict, bool]] = []

    def publish(self, topic, payload, qos=1, retain=False):
        self.published.append((topic, json.loads(payload), retain))
        return _Info()

    def subscribe(self, topic):
        pass


def test_config_is_published_retained_on_connect(ctx) -> None:
    context, _ = ctx
    client = _Client()
    context.mqtt_service._client = client
    context.mqtt_service._on_connect(client, None, None, 0, None)

    config_msgs = {t: (p, r) for t, p, r in client.published if t.startswith("nova/config/")}
    assert set(config_msgs) == {"nova/config/novaGround", "nova/config/novaThermo"}
    payload, retained = config_msgs["nova/config/novaThermo"]
    assert retained is True
    assert payload["config_hash"] == context.config_service.active_hash
    assert payload["gpio"]["inputs"] == [5]


@pytest.fixture
def station(ctx, tmp_path):
    context, _ = ctx
    client = _Client()
    context.mqtt_service._client = client
    context.mqtt_service._connected = True
    sent: list[dict] = []

    async def record(payload: dict) -> None:
        sent.append(payload)

    context.broadcast = record  # type: ignore[method-assign]
    app = FastAPI()
    app.state.ctx = context
    app.include_router(config_router.router)
    return TestClient(app), context, client, sent


def test_config_change_republishes_and_rebroadcasts_status(station, tmp_path) -> None:
    http, context, client, sent = station
    (tmp_path / "other.yaml").write_text(
        "Actuators:\n- name: H\n  type: gpio_device\n  binding: {target: TCS, gpio_channel: 23}\n",
        encoding="utf-8")
    context.runtime.pi_config_status["novaThermo"] = _status(config_hash=context.config_service.active_hash)

    assert http.post("/api/config/load", params={"path": "other.yaml"}).status_code == 200

    thermo = [p for t, p, r in client.published if t == "nova/config/novaThermo" and r]
    assert thermo[-1]["gpio"]["outputs"] == {"23": {"max_on_ms": None, "on_comms_loss": "hold"}}
    assert thermo[-1]["config_hash"] == context.config_service.active_hash
    status = {m["node"]: m["status"] for m in sent if m["type"] == "pi_config_status"}
    # The Pi still reports the old hash, so it now shows as a mismatch.
    assert status["novaThermo"]["mismatch_reasons"] == ["config_hash"]


def test_edit_republishes_the_config(station) -> None:
    http, context, client, _ = station
    current = http.get("/api/config/").json()
    current["Heartbeat"] = {"timeout_ms": 3000}
    assert http.put("/api/config/update", json=current).status_code == 200
    ground = [p for t, p, r in client.published if t == "nova/config/novaGround"]
    assert ground[-1]["heartbeat"] == {"timeout_ms": 3000}
    assert ground[-1]["config_hash"] == context.config_service.active_hash


def test_edit_with_a_reserved_pin_is_refused(station) -> None:
    http, _, client, _ = station
    current = http.get("/api/config/").json()
    current["Actuators"][0]["binding"]["gpio_channel"] = 8
    response = http.put("/api/config/update", json=current)
    assert response.status_code == 400 and "reserved" in response.json()["detail"]
    assert client.published == []


def test_rest_endpoint_exposes_config_and_status(station) -> None:
    http, context, _, _ = station
    context.runtime.pi_config_status["novaThermo"] = _status(
        config_hash=context.config_service.active_hash, tripped=[_trip("gpio:17")])
    body = http.get("/api/config/pi").json()
    assert body["reserved_pins"] == [0, 1, 2, 3, 7, 8, 9, 10, 11, 12, 13, 16, 20, 21, 26]
    assert body["gpio_max"] == 27 and body["tc_types"] == ["J", "K", "T", "E", "R", "S", "B", "N"]
    thermo = body["nodes"]["novaThermo"]
    assert thermo["target"] == "TCS" and thermo["topic"] == "nova/config/novaThermo"
    assert thermo["config"]["daq"] == {"1": {"0": "K", "1": "T"}}
    assert thermo["status"]["mismatch"] is False
    assert thermo["status"]["tripped"][0]["actuators"] == ["TANK-HTR-PWR"]
    assert body["nodes"]["novaGround"]["status"]["received"] is False


def test_get_config_shows_the_new_fields(station) -> None:
    http, *_ = station
    body = http.get("/api/config/").json()
    assert body["Heartbeat"] == {"timeout_ms": 2000}
    sensors = {s["name"]: s for s in body["Sensors"]}
    assert sensors["TC_A"]["tc_type"] == "T" and sensors["TC_B"]["tc_type"] is None
    assert sensors["DOOR"]["binding"] == {"source": "TCS", "gpio_channel": 5}
    actuators = {a["name"]: a for a in body["Actuators"]}
    assert actuators["TANK-HTR-PWR"]["actions"]["max_on_s"] == 1800
    assert actuators["TANK-HTR-PWR"]["actions"]["on_comms_loss"] == "safe"
    assert actuators["Test_Sol"]["actions"]["on_comms_loss"] == "hold"
