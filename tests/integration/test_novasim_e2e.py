"""novaSim -> MQTT -> backend -> websocket client, with real processes.

The backend runs in-process (TestClient), but talks to a real Mosquitto
broker that novaSim publishes to, so the MQTT contract between the
simulator and the backend is exercised for real.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

pytestmark = pytest.mark.integration

# One sensor from each side that novaSim simulates:
#   PGSO: GCS hat 0 / channel 0, raw 0.99-3.99 V, calibrated here to 99-399 psi.
#   PCC:  FAS EPB_0 / channel 1, raw 0.2-0.9, passed through unconverted.
TEST_CONFIG = """
Sensors:
  - name: PGSO
    type: PT
    unit: psi
    binding:
      source: GCS
      hat_id: 0
      channel_id: 0
    convert:
      method: linear
      calibration:
        - [0.0, 0.0]
        - [5.0, 500.0]
  - name: PCC
    type: PT
    unit: V
    binding:
      source: FAS
      node: EPB_0
      channel: 1
    convert:
      method: none
Actuators: []
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


def _wait_for_engine_data(ctx) -> None:
    """Poll until both test sensors have arrived over MQTT.

    Polling first means a broken pipeline fails with a clear timeout instead
    of hanging forever in websocket.receive_json().
    """
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        names = {row.get("name") for row in ctx.runtime.latest_engine_data or []}
        if {"PGSO", "PCC"} <= names:
            return
        time.sleep(0.1)
    raise AssertionError(
        f"No engine_data for PGSO and PCC within {TIMEOUT_S}s; "
        f"latest_engine_data={ctx.runtime.latest_engine_data!r}"
    )


def _receive_until(ws, msg_type: str, max_messages: int = 500) -> dict:
    for _ in range(max_messages):
        msg = ws.receive_json()
        if msg.get("type") == msg_type:
            return msg
    raise AssertionError(f"No {msg_type!r} message in {max_messages} websocket messages")


def _by_name(engine_data: dict) -> dict[str, dict]:
    return {row["name"]: row for row in engine_data["data"]}


def test_websocket_client_receives_calibrated_engine_telemetry(backend_env, nova_sim) -> None:
    app = create_app()
    with TestClient(app) as client:
        _wait_for_engine_data(app.state.ctx)

        with client.websocket_connect("/ws") as ws:
            # The connect-time snapshot, then a live broadcast from MQTT.
            snapshot = _receive_until(ws, "engine_data")
            live = _receive_until(ws, "engine_data")

    for message in (snapshot, live):
        sensors = _by_name(message)

        pgso = sensors["PGSO"]
        assert pgso["unit"] == "psi"
        # Raw 0.99-3.99 V through the x100 calibration (plus a little noise).
        assert 90.0 <= pgso["value"] <= 410.0, pgso

        pcc = sensors["PCC"]
        assert pcc["unit"] == "V"
        assert 0.15 <= pcc["value"] <= 0.95, pcc
