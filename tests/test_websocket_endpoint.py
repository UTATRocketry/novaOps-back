"""The /ws endpoint: connect sequence, fan-out, roles, commands and bad input."""
from contextlib import ExitStack

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.context import AppContext
from app.routers import websocket as ws_router
from app.services.role_service import ClientRole

CONFIG = """
Sensors: []
Actuators:
  - name: SVVENT
    type: solenoid
    binding: {target: GCS, relay_channel: 2}
safetyRules:
  critical:
    - SVVENT: [open]
"""


@pytest.fixture
def station(tmp_path):
    path = tmp_path / "system.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    ctx = AppContext(path, tmp_path / "data")
    ctx.runtime.initialize_all(ctx.config_service.config)
    commands: list[dict] = []
    console: list[dict] = []
    ctx.mqtt_service.publish_device_commands = commands.extend  # type: ignore[method-assign]
    ctx.mqtt_service.publish_console = console.append  # type: ignore[method-assign]
    ctx.mqtt_service.publish_console_command = console.append  # type: ignore[method-assign]
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(ws_router.router)
    # `with` starts the portal that http.portal.call() uses to run coroutines
    # on the app's event loop.
    with TestClient(app) as http:
        yield http, ctx, commands, console


def _drain_connect(ws) -> list[dict]:
    """Read the messages sent on connect, up to and including the lockout state."""
    messages = []
    while True:
        message = ws.receive_json()
        messages.append(message)
        if message["type"] == "lockout":
            return messages


def _set_role(ctx: AppContext, client_id: str, role: str) -> None:
    ctx.role_service._roles[client_id] = ClientRole.from_str(role)


def test_connect_sequence_is_session_then_snapshot_then_cached_data_then_lockout(station) -> None:
    http, ctx, *_ = station
    ctx.runtime.latest_engine_data = [{"name": "P", "value": 1.0}]
    ctx.runtime.latest_sensors = ctx.runtime.latest_engine_data
    ctx.runtime.latest_flight_data = {"altitude": 3}
    ctx.runtime.latest_events = [{"e": 1}]

    with http.websocket_connect("/ws") as ws:
        messages = _drain_connect(ws)

    assert [m["type"] for m in messages] == [
        "session", "snapshot", "parsed_data", "engine_data", "flight_data", "flight_events", "lockout",
    ]
    assert messages[0]["role"] == "viewer"
    assert messages[0]["client_id"] == messages[1]["client_id"]
    assert set(messages[1]["actuator_states"]) == {"SVVENT"}


def test_empty_cache_sends_only_session_snapshot_and_lockout(station) -> None:
    http, *_ = station
    with http.websocket_connect("/ws") as ws:
        assert [m["type"] for m in _drain_connect(ws)] == ["session", "snapshot", "lockout"]


def test_clients_are_listed_and_removed_on_disconnect(station) -> None:
    http, ctx, *_ = station
    with http.websocket_connect("/ws") as a, http.websocket_connect("/ws") as b:
        _drain_connect(a)
        _drain_connect(b)
        assert len(ctx.ws_manager.list_client_ids()) == 2
    assert ctx.ws_manager.list_client_ids() == []
    assert ctx.role_service.list_clients() == []


@pytest.mark.parametrize("count", [1, 5, 20])
def test_broadcast_reaches_every_client_in_order(station, count) -> None:
    http, ctx, *_ = station

    with ExitStack() as stack:
        sockets = [stack.enter_context(http.websocket_connect("/ws")) for _ in range(count)]
        for ws in sockets:
            _drain_connect(ws)

        for n in range(3):
            http.portal.call(ctx.broadcast, {"type": "tick", "n": n})

        for ws in sockets:
            assert [ws.receive_json()["n"] for _ in range(3)] == [0, 1, 2]


def test_viewer_command_gets_an_error_and_publishes_nothing(station) -> None:
    http, ctx, commands, _ = station
    with http.websocket_connect("/ws") as ws:
        _drain_connect(ws)
        ws.send_json({"type": "solenoid", "name": "SVVENT", "state": "open"})
        error = ws.receive_json()

    assert error["type"] == "error"
    assert "Insufficient role" in error["detail"]
    assert commands == []


def test_operator_command_is_published_and_broadcast(station) -> None:
    http, ctx, commands, _ = station
    with http.websocket_connect("/ws") as ws:
        client_id = _drain_connect(ws)[0]["client_id"]
        _set_role(ctx, client_id, "operator")
        ws.send_json({"type": "solenoid", "name": "SVVENT", "state": "open"})
        update = ws.receive_json()

    assert update["type"] == "actuator_states"
    assert update["actuator_states"]["SVVENT"] == {"position": "open"}
    assert len(commands) == 1


def test_invalid_command_body_gets_an_error_and_keeps_the_socket_open(station) -> None:
    http, ctx, commands, _ = station
    with http.websocket_connect("/ws") as ws:
        client_id = _drain_connect(ws)[0]["client_id"]
        _set_role(ctx, client_id, "operator")
        ws.send_json({"type": "solenoid", "name": "NOPE", "state": "open"})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"nonsense": True})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "solenoid", "name": "SVVENT", "state": "open"})
        assert ws.receive_json()["type"] == "actuator_states"
    assert len(commands) == 1


def test_console_command_needs_operator(station) -> None:
    http, ctx, _, console = station
    with http.websocket_connect("/ws") as ws:
        client_id = _drain_connect(ws)[0]["client_id"]
        ws.send_json({"type": "console_command", "command": {"action": "start"}})
        assert "Insufficient role" in ws.receive_json()["detail"]
        assert console == []

        _set_role(ctx, client_id, "operator")
        ws.send_json({"type": "console_command", "command": {"action": "bogus"}})
        assert "Unknown console action" in ws.receive_json()["detail"]
        ws.send_json({"type": "console_command", "command": {"action": "start"}})
        ws.send_json({"type": "role_request", "role": "viewer"})
        ws.receive_json()  # the role change reply proves the start was processed first

    assert console == [{"action": "start"}]


def test_role_request_changes_own_role_and_pushes_it(station) -> None:
    http, ctx, *_ = station
    with http.websocket_connect("/ws") as ws:
        client_id = _drain_connect(ws)[0]["client_id"]
        ws.send_json({"type": "role_request", "role": "operator"})
        pushed = ws.receive_json()

    assert pushed == {"type": "session", "role": "operator", "client_id": client_id}


def test_role_change_is_pushed_to_the_right_client_only(station, monkeypatch) -> None:
    http, ctx, *_ = station
    monkeypatch.setenv("NOVA_ADMIN_PASSWORD", "pw")
    with http.websocket_connect("/ws") as admin, http.websocket_connect("/ws") as other:
        _drain_connect(admin)
        other_id = _drain_connect(other)[0]["client_id"]
        admin.send_json({"type": "role_request", "role": "admin", "password": "pw"})
        admin.receive_json()

        admin.send_json({"type": "role_request", "role": "operator", "target_client_id": other_id})
        pushed = other.receive_json()

    assert pushed["role"] == "operator" and pushed["client_id"] == other_id


def test_role_request_errors(station, monkeypatch) -> None:
    http, ctx, *_ = station
    monkeypatch.delenv("NOVA_ADMIN_PASSWORD", raising=False)
    with http.websocket_connect("/ws") as ws:
        _drain_connect(ws)
        ws.send_json({"type": "role_request", "role": "root"})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "role_request", "role": "admin", "password": "x"})
        assert "disabled" in ws.receive_json()["detail"]
        ws.send_json({"type": "role_request", "role": "operator", "target_client_id": "someone-else"})
        assert "Only admin" in ws.receive_json()["detail"]


def test_dead_client_is_dropped_without_breaking_the_others(station) -> None:
    http, ctx, *_ = station
    with http.websocket_connect("/ws") as good:
        _drain_connect(good)

        class Dead:
            async def accept(self): ...
            async def send_json(self, payload): raise RuntimeError("gone")

        async def add_dead() -> str:
            dead_id = await ctx.ws_manager.connect(Dead())
            ctx.role_service.on_connect(dead_id)
            return dead_id

        dead_id = http.portal.call(add_dead)
        http.portal.call(ctx.broadcast, {"type": "tick"})

        assert good.receive_json() == {"type": "tick"}
        assert dead_id not in ctx.ws_manager.list_client_ids()
        assert dead_id not in [c["client_id"] for c in ctx.role_service.list_clients()]


@pytest.mark.parametrize("junk", ["not json at all", "[1, 2, 3]", "null", '"text"'])
def test_garbage_frames_do_not_leak_the_client(station, junk) -> None:
    http, ctx, *_ = station
    try:
        with http.websocket_connect("/ws") as ws:
            _drain_connect(ws)
            ws.send_text(junk)
            ws.receive_json()  # either an error reply or the socket closing
    except Exception:  # noqa: BLE001 - a closed/errored socket is acceptable here
        pass
    assert ctx.ws_manager.list_client_ids() == [], "client left registered after a bad frame"
    assert ctx.role_service.list_clients() == []
