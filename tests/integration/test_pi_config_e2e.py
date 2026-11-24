"""Backend -> nova/config/novaGround -> novaSim -> .../status -> backend, for real.

Checks the retained config message and its status round trip through a real
Mosquitto broker, with novaSim standing in for novaGround.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

pytestmark = pytest.mark.integration

TEST_CONFIG = """
Sensors:
  - name: TC1
    type: TC
    binding: {source: GCS, hat_id: 1, channel_id: 0}
    tc_type: T
  - name: DOOR
    type: DI
    binding: {source: GCS, gpio_channel: 5}
Actuators:
  - name: HTR
    type: gpio_device
    binding: {target: GCS, gpio_channel: 17}
    actions: {max_on_s: 600, on_comms_loss: safe}
"""

TIMEOUT_S = 15


@pytest.fixture
def backend_env(tmp_path, monkeypatch, mqtt_broker):
    host, port = mqtt_broker
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "system.yaml").write_text(TEST_CONFIG, encoding="utf-8")
    monkeypatch.setenv("NOVA_MQTT_BROKER", host)
    monkeypatch.setenv("NOVA_MQTT_PORT", str(port))
    monkeypatch.setenv("NOVA_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("NOVA_CONFIG_HISTORY_DIR", str(tmp_path / "history"))
    monkeypatch.setenv("NOVA_DATA_DIR", str(tmp_path / "data"))


def _wait(predicate, what: str):
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"Timed out after {TIMEOUT_S}s waiting for {what}")


def test_novasim_applies_the_config_and_reports_back(backend_env, nova_sim) -> None:
    app = create_app()
    with TestClient(app) as client:
        ctx = app.state.ctx
        status = _wait(lambda: ctx.runtime.pi_config_status.get("novaGround"), "novaGround config status")
        _wait(lambda: ctx.runtime.pi_config_status["novaGround"].get("config_hash")
              == ctx.config_service.active_hash, "status for the active config")

        body = client.get("/api/config/pi").json()
        view = body["nodes"]["novaGround"]["status"]
        assert view["received"] and view["mismatch"] is False, view
        assert view["thermocouples"] == {"1": {"0": "T"}}
        assert body["nodes"]["novaGround"]["config"]["gpio"] == {
            "outputs": {"17": {"max_on_ms": 600000, "on_comms_loss": "safe"}}, "inputs": [5]}
        assert status["reserved_pins"]

        # novaSim reports the configured input in its gpios, which becomes a DI reading.
        _wait(lambda: any(row.get("name") == "DOOR" for row in ctx.runtime.latest_engine_data or []),
              "DOOR DI reading")
        door = next(row for row in ctx.runtime.latest_engine_data if row["name"] == "DOOR")
        assert door["value"] in (0, 1)
