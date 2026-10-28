"""Config REST routes, hash stability and the effect of a config switch."""
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.context import AppContext
from app.routers import config as config_router
from app.services.config_history import content_hash

GOLDEN = Path(__file__).parent / "golden" / "config_hash_vectors.json"

CONFIG_A = """
Sensors: []
Actuators:
  - name: VALVE_A
    type: solenoid
    binding: {target: GCS, relay_channel: 1}
"""

CONFIG_B = """
Sensors: []
Actuators:
  - name: VALVE_B
    type: solenoid
    binding: {target: GCS, relay_channel: 2}
"""


@pytest.fixture
def station(tmp_path):
    path = tmp_path / "system.yaml"
    path.write_text(CONFIG_A, encoding="utf-8")
    (tmp_path / "other.yaml").write_text(CONFIG_B, encoding="utf-8")
    ctx = AppContext(path, tmp_path / "data")
    ctx.runtime.initialize_all(ctx.config_service.config)
    sent: list[dict] = []

    async def record(payload: dict) -> None:
        sent.append(payload)

    ctx.broadcast = record  # type: ignore[method-assign]
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(config_router.router)
    return TestClient(app), ctx, sent, tmp_path


# --- hash scheme (AGENTS.md rule 9: must equal Get-ContentHash in Nova.ps1) ---

@pytest.mark.parametrize("vector", json.loads(GOLDEN.read_text())["vectors"], ids=lambda v: v["name"])
def test_content_hash_matches_golden_vectors(vector) -> None:
    assert content_hash(bytes.fromhex(vector["hex"])) == vector["hash"]


# --- read routes ---

def test_get_config_and_listings(station) -> None:
    http, ctx, _, _ = station
    assert http.get("/api/config/").status_code == 200
    assert [a["name"] for a in http.get("/api/config/actuators").json()] == ["VALVE_A"]
    assert http.get("/api/config/sensors").json() == []
    assert http.get("/api/config/procedures").json() == []
    assert sorted(http.get("/api/config/list").json()) == ["other.yaml", "system.yaml"]


def test_download(station) -> None:
    http, _, _, tmp_path = station
    assert http.get("/api/config/download/other.yaml").content == (tmp_path / "other.yaml").read_bytes()
    assert http.get("/api/config/download/missing.yaml").status_code == 404
    assert http.get("/api/config/download/other.txt").status_code == 400


# --- switching configs ---

def test_load_switches_actuators_and_tells_every_client(station) -> None:
    """Regression for Nova-Collected#23."""
    http, ctx, sent, _ = station
    ctx.runtime.actuator_states["VALVE_A"]["position"] = "open"

    assert http.post("/api/config/load", params={"path": "other.yaml"}).status_code == 200

    assert set(ctx.runtime.actuator_states) == {"VALVE_B"}
    assert [m["type"] for m in sent] == ["config_update", "actuator_states"]
    assert set(sent[1]["actuator_states"]) == {"VALVE_B"}
    assert http.get("/api/config/actuators").json()[0]["name"] == "VALVE_B"


def test_load_missing_file_changes_nothing(station) -> None:
    http, ctx, sent, _ = station
    assert http.post("/api/config/load", params={"path": "nope.yaml"}).status_code == 404
    assert sent == []
    assert set(ctx.runtime.actuator_states) == {"VALVE_A"}


def test_upload_activates_the_file(station) -> None:
    http, ctx, sent, tmp_path = station
    response = http.post("/api/config/upload", files={"file": ("new.yaml", CONFIG_B, "application/x-yaml")})

    assert response.status_code == 200
    assert (tmp_path / "new.yaml").read_text() == CONFIG_B
    assert ctx.config_service.config_path.name == "new.yaml"
    assert set(ctx.runtime.actuator_states) == {"VALVE_B"}


def test_upload_strips_directories_from_the_name(station) -> None:
    http, ctx, _, tmp_path = station
    http.post("/api/config/upload", files={"file": ("../escape.yaml", CONFIG_B, "application/x-yaml")})
    assert not (tmp_path.parent / "escape.yaml").exists()
    assert (tmp_path / "escape.yaml").exists()


def test_invalid_upload_does_not_replace_the_active_config(station) -> None:
    http, ctx, sent, tmp_path = station
    before = (tmp_path / "system.yaml").read_text()

    response = http.post("/api/config/upload", files={"file": ("system.yaml", "Actuators: [[[", "text/plain")})

    assert response.status_code == 400
    assert (tmp_path / "system.yaml").read_text() == before, "a bad upload overwrote the live config file"
    assert set(ctx.runtime.actuator_states) == {"VALVE_A"}


def test_duplicate_actuator_names_are_rejected(station) -> None:
    http, ctx, sent, _ = station
    duplicate = {"Actuators": [
        {"name": "X", "type": "solenoid", "binding": {"target": "GCS", "relay_channel": 1}},
        {"name": "X", "type": "solenoid", "binding": {"target": "GCS", "relay_channel": 2}},
    ]}
    response = http.put("/api/config/update", json=duplicate)
    assert response.status_code == 400
    assert "Duplicate actuator" in response.json()["detail"]
    assert http.patch("/api/config/update", json=duplicate).status_code == 400
    assert set(ctx.runtime.actuator_states) == {"VALVE_A"}


def test_put_replaces_and_patch_merges(station) -> None:
    http, ctx, sent, _ = station
    replaced = {"Actuators": [{"name": "ONLY", "type": "solenoid", "binding": {"target": "GCS", "relay_channel": 3}}]}
    assert http.put("/api/config/update", json=replaced).status_code == 200
    assert set(ctx.runtime.actuator_states) == {"ONLY"}

    patched = http.patch("/api/config/update", json={"Sensors": []})
    assert patched.status_code == 200
    assert set(ctx.runtime.actuator_states) == {"ONLY"}  # untouched by the patch


def test_reload_picks_up_a_hand_edit(station) -> None:
    http, ctx, _, tmp_path = station
    (tmp_path / "system.yaml").write_text(CONFIG_B, encoding="utf-8")

    assert http.post("/api/config/reload").status_code == 200

    assert set(ctx.runtime.actuator_states) == {"VALVE_B"}


def test_loading_a_broken_file_keeps_the_running_config_and_path(station) -> None:
    http, ctx, sent, tmp_path = station
    (tmp_path / "broken.yaml").write_text("Actuators: [[[", encoding="utf-8")

    assert http.post("/api/config/load", params={"path": "broken.yaml"}).status_code == 400

    assert ctx.config_service.config_path.name == "system.yaml"
    assert set(ctx.runtime.actuator_states) == {"VALVE_A"}
    assert sent == []
