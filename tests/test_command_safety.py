"""Safety gating for actuator commands: novaLock, hazardous rules and roles.

These drive the real routers and AppContext, with MQTT publishing replaced by
a list, so a rejected command is proven to publish nothing.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.context import AppContext
from app.models import SystemConfig
from app.routers import commands, roles

CONFIG = """
Actuators:
  - name: SVFILL
    type: solenoid
    binding: {target: GCS, relay_channel: 1}
  - name: SVVENT
    type: solenoid
    binding: {target: GCS, relay_channel: 2}
  - name: IGNITER
    type: gpio_device
    binding: {target: GCS, gpio_channel: 3}
safetyRules:
  hazardous:
    - SVFILL: [open]
    - IGNITER: ALL
  critical:
    - SVVENT: [open]
"""


@pytest.fixture
def station(tmp_path):
    config_path = tmp_path / "system.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    ctx = AppContext(config_path, tmp_path / "data")
    published: list[dict] = []
    ctx.mqtt_service.publish_device_commands = published.extend  # type: ignore[method-assign]
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(commands.router)
    app.include_router(roles.router)
    return TestClient(app), ctx, published


def _command(client: TestClient, name: str, state: str, client_id: str | None = "c1"):
    headers = {"X-Client-Id": client_id} if client_id else {}
    return client.post("/api/commands", json={"type": "x", "name": name, "state": state}, headers=headers)


def _set_role(ctx: AppContext, client_id: str, role: str) -> None:
    from app.services.role_service import ClientRole

    ctx.role_service._roles[client_id] = ClientRole.from_str(role)


# --- hazardous rules vs novaLock ---

@pytest.mark.parametrize(
    ("name", "state", "blocked_when_locked"),
    [
        ("SVFILL", "open", True),
        ("SVFILL", "OPEN", True),     # rule matching ignores case
        ("SVFILL", "closed", False),  # only the listed state is hazardous
        ("IGNITER", "arm", True),     # ALL covers every state
        ("IGNITER", "disarm", True),
        ("SVVENT", "open", False),    # critical is not hazardous
    ],
)
def test_hazardous_commands_follow_the_lock(station, name, state, blocked_when_locked) -> None:
    client, ctx, published = station
    _set_role(ctx, "c1", "operator")

    ctx.runtime.lockout_state = "locked"
    response = _command(client, name, state)
    if blocked_when_locked:
        assert response.status_code == 400
        assert "locked" in response.json()["detail"]
        assert published == []
    else:
        assert response.status_code == 200
        assert published

    published.clear()
    ctx.runtime.lockout_state = "unlocked"
    assert _command(client, name, state).status_code == 200
    assert published


@pytest.mark.parametrize("state", ["locked", "", "unknown", "LOCKED", "novaLock offline"])
def test_anything_but_unlocked_counts_as_locked(station, state) -> None:
    client, ctx, published = station
    _set_role(ctx, "c1", "operator")
    ctx.runtime.lockout_state = state

    assert _command(client, "SVFILL", "open").status_code == 400
    assert published == []


def test_unlocked_state_is_case_and_space_insensitive(station) -> None:
    client, ctx, published = station
    _set_role(ctx, "c1", "operator")
    ctx.runtime.lockout_state = "  Unlocked "

    assert _command(client, "SVFILL", "open").status_code == 200


def test_admin_is_also_blocked_by_the_lock(station) -> None:
    client, ctx, published = station
    _set_role(ctx, "c1", "admin")
    ctx.runtime.lockout_state = "locked"

    assert _command(client, "SVFILL", "open").status_code == 400
    assert published == []


# --- roles ---

@pytest.mark.parametrize("client_id", [None, "unknown-client", "viewer1"])
def test_viewers_cannot_send_any_command(station, client_id) -> None:
    client, ctx, published = station
    _set_role(ctx, "viewer1", "viewer")

    for name, state in [("SVVENT", "open"), ("SVFILL", "closed")]:
        response = _command(client, name, state, client_id)
        assert response.status_code == 400
        assert "Insufficient role" in response.json()["detail"]
    assert published == []


def test_pad_may_only_send_critical_commands(station) -> None:
    client, ctx, published = station
    _set_role(ctx, "c1", "pad")

    assert _command(client, "SVVENT", "open").status_code == 200
    assert len(published) == 1
    published.clear()

    assert _command(client, "SVVENT", "closed").status_code == 400  # state not listed
    assert _command(client, "SVFILL", "closed").status_code == 400  # actuator not critical
    assert published == []


@pytest.mark.parametrize("role", ["operator", "admin"])
def test_operator_and_admin_can_send(station, role) -> None:
    client, ctx, published = station
    _set_role(ctx, "c1", role)

    assert _command(client, "SVVENT", "closed").status_code == 200
    assert published


# --- malformed requests publish nothing ---

def test_unknown_actuator_is_rejected(station) -> None:
    client, ctx, published = station
    _set_role(ctx, "c1", "operator")

    response = _command(client, "NOPE", "open")

    assert response.status_code == 400
    assert "NOPE" in response.json()["detail"]
    assert published == []
    assert "NOPE" not in ctx.runtime.actuator_states


def test_unsupported_servo_state_is_rejected() -> None:
    config = SystemConfig.model_validate(
        {
            "Actuators": [
                {
                    "name": "BVGSO",
                    "type": "servo",
                    "binding": {"target": "GCS", "servo_channel": 8},
                    "actions": {"position_aliases": ["open"], "positions": [900]},
                }
            ]
        }
    )
    from app.models import CommandPayload
    from app.services.command_service import CommandParser

    with pytest.raises(ValueError, match="Unsupported servo state"):
        CommandParser(config).parse(CommandPayload(type="servo", name="BVGSO", state="sideways"))


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"type": "x", "name": "SVVENT"},
        {"type": "x", "state": "open"},
        {"type": "x", "name": 5, "state": "open"},
        {"type": "x", "name": "SVVENT", "state": ["open"]},
    ],
)
def test_malformed_body_is_a_4xx_and_publishes_nothing(station, body) -> None:
    client, ctx, published = station
    _set_role(ctx, "c1", "operator")

    response = client.post("/api/commands", json=body, headers={"X-Client-Id": "c1"})

    assert 400 <= response.status_code < 500
    assert published == []


# --- command state tracking ---

def test_accepted_command_updates_actuator_state(station) -> None:
    client, ctx, published = station
    _set_role(ctx, "c1", "operator")
    ctx.runtime.initialize_all(ctx.config_service.config)

    assert _command(client, "SVVENT", "open").status_code == 200

    assert ctx.runtime.actuator_states["SVVENT"] == {"position": "open"}


def test_rejected_command_leaves_actuator_state_alone(station) -> None:
    client, ctx, published = station
    _set_role(ctx, "c1", "operator")
    ctx.runtime.initialize_all(ctx.config_service.config)
    ctx.runtime.lockout_state = "locked"

    assert _command(client, "SVFILL", "open").status_code == 400

    assert ctx.runtime.actuator_states["SVFILL"] == {"position": "closed"}


# --- roles router ---

def _register(ctx: AppContext, client_id: str, role: str = "viewer") -> None:
    ctx.role_service.on_connect(client_id)
    _set_role(ctx, client_id, role)


def _assign(client: TestClient, role: str, caller: str | None, **extra):
    headers = {"X-Client-Id": caller} if caller else {}
    return client.post("/api/roles", json={"role": role, **extra}, headers=headers)


def test_admin_role_needs_password_configured(station, monkeypatch) -> None:
    client, ctx, _ = station
    _register(ctx, "c1")
    monkeypatch.delenv("NOVA_ADMIN_PASSWORD", raising=False)

    response = _assign(client, "admin", "c1", password="anything")

    assert response.status_code == 503
    assert ctx.role_service.get_role("c1").name == "viewer"


def test_admin_role_needs_the_right_password(station, monkeypatch) -> None:
    client, ctx, _ = station
    _register(ctx, "c1")
    monkeypatch.setenv("NOVA_ADMIN_PASSWORD", "s3cret")

    assert _assign(client, "admin", "c1", password="wrong").status_code == 403
    assert _assign(client, "admin", "c1").status_code == 403
    assert ctx.role_service.get_role("c1").name == "viewer"

    assert _assign(client, "admin", "c1", password="s3cret").status_code == 200
    assert ctx.role_service.get_role("c1").name == "admin"


def test_only_admin_can_assign_roles_to_others(station) -> None:
    client, ctx, _ = station
    _register(ctx, "op", "operator")
    _register(ctx, "v")

    response = _assign(client, "operator", "op", target_client_id="v")

    assert response.status_code == 403
    assert ctx.role_service.get_role("v").name == "viewer"


def test_admin_can_assign_roles_to_others(station) -> None:
    client, ctx, _ = station
    _register(ctx, "boss", "admin")
    _register(ctx, "v")

    response = _assign(client, "operator", "boss", target_client_id="v")

    assert response.status_code == 200
    assert ctx.role_service.get_role("v").name == "operator"


@pytest.mark.parametrize(
    ("role", "caller", "expected"),
    [("root", "c1", 400), ("operator", None, 400), ("operator", "ghost", 404)],
)
def test_role_assignment_errors(station, role, caller, expected) -> None:
    client, ctx, _ = station
    _register(ctx, "c1")

    assert _assign(client, role, caller).status_code == expected
