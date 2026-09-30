"""Station config history and recording metadata.

Every config the backend runs with is snapshotted by content hash, and every
recording's <name>.meta.json names the config(s) its raw data was taken with -
including changes made in the middle of a recording.
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.context import AppContext
from app.routers import config as config_router
from app.services.config_history import ConfigHistory, content_hash
from app.services.recording_service import recording_files

CONFIG = """
Sensors: []
Actuators: []
"""


def _context(tmp_path, env: str = "dev") -> AppContext:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "system.yaml").write_text(CONFIG, encoding="utf-8")
    ctx = AppContext(
        config_dir / "system.yaml",
        tmp_path / "data",
        history_dir=tmp_path / "history",
        env=env,
        software={"release": "v2026.10.04", "backend_commit": "abc123"},
    )
    ctx.mqtt_service.publish_data_saving = lambda *a, **k: None  # type: ignore[method-assign]
    ctx.config_service.reload()
    ctx.config_service.record_active("startup")
    return ctx


def _history(tmp_path) -> list[dict]:
    lines = (tmp_path / "history" / "history.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def _meta(ctx: AppContext, name: str) -> dict:
    return json.loads(ctx.recordings.meta_path(name).read_text(encoding="utf-8"))


def test_history_stores_each_content_once_and_logs_changes(tmp_path) -> None:
    path = tmp_path / "system.yaml"
    history = ConfigHistory(tmp_path / "history", env="prod")

    path.write_bytes(b"a: 1\n")
    h1 = history.snapshot(path, "startup")
    h1_again = history.snapshot(path, "edit")          # identical autosave
    path.write_bytes(b"a: 2\n")
    h2 = history.snapshot(path, "edit", client="c1 (admin)")

    assert h1 == h1_again == content_hash(b"a: 1\n")
    assert h2 != h1
    snaps = sorted(p.name for p in (tmp_path / "history" / "snapshots").iterdir())
    assert snaps == sorted([f"{h1}.yaml", f"{h2}.yaml"])
    entries = _history(tmp_path)
    assert [(e["source"], e["hash"]) for e in entries] == [("startup", h1), ("edit", h2)]
    assert entries[1]["client"] == "c1 (admin)" and entries[1]["env"] == "prod"


def test_history_without_dir_still_hashes_but_writes_nothing(tmp_path) -> None:
    path = tmp_path / "system.yaml"
    path.write_bytes(b"a: 1\n")
    assert ConfigHistory(None).snapshot(path, "startup") == content_hash(b"a: 1\n")
    assert list(tmp_path.iterdir()) == [path]


def test_recording_meta_records_start_config_and_mid_recording_changes(tmp_path) -> None:
    ctx = _context(tmp_path)
    startup_hash = _history(tmp_path)[0]["hash"]

    name = ctx.set_data_saving(True)
    assert name is not None and name.endswith("_data_0")
    meta = _meta(ctx, name)
    assert meta["env"] == "dev"
    assert meta["release"] == "v2026.10.04" and meta["backend_commit"] == "abc123"
    assert meta["stopped"] is None
    assert meta["config"] == [
        {"file": "system.yaml", "hash": startup_hash, "at": meta["config"][0]["at"], "source": "start"}
    ]

    # Someone recalibrates while data is being taken.
    ctx.config_service.update_config({"Sensors": [], "Actuators": [], "safetyRules": {}}, client="c1")
    ctx.set_data_saving(False)

    meta = _meta(ctx, name)
    assert meta["stopped"] is not None
    assert [c["source"] for c in meta["config"]] == ["start", "edit"]
    new_hash = meta["config"][1]["hash"]
    assert (tmp_path / "history" / "snapshots" / f"{new_hash}.yaml").exists()
    assert ctx.recordings.active is None


def test_config_changes_outside_a_recording_do_not_touch_old_meta(tmp_path) -> None:
    ctx = _context(tmp_path)
    name = ctx.set_data_saving(True)
    ctx.set_data_saving(False)
    before = _meta(ctx, name)
    ctx.config_service.update_config({"Sensors": [], "Actuators": [], "safetyRules": {}})
    assert _meta(ctx, name) == before


def test_start_while_recording_closes_the_previous_one(tmp_path) -> None:
    ctx = _context(tmp_path)
    first = ctx.set_data_saving(True)
    second = ctx.set_data_saving(True)
    assert first != second
    assert _meta(ctx, first)["stopped"] is not None
    assert _meta(ctx, second)["stopped"] is None


def test_recording_names_never_reuse_files_left_on_disk(tmp_path) -> None:
    """A backend restart used to reset the counter and overwrite _data_0."""
    ctx = _context(tmp_path)
    first = ctx.set_data_saving(True)
    ctx.set_data_saving(False)
    (ctx.data_dir / f"{first}_sensors.csv").write_text("x", encoding="utf-8")

    restarted = _context(tmp_path / "restart")
    restarted.recordings._data_dir = ctx.data_dir  # same data dir, fresh process
    restarted.data_dir = ctx.data_dir
    assert restarted.set_data_saving(True).endswith("_data_1")


def test_recording_files_does_not_claim_longer_names(tmp_path) -> None:
    for fname in ["r_data_1.csv", "r_data_1_sensors.csv", "r_data_1.meta.json", "r_data_10_sensors.csv"]:
        (tmp_path / fname).write_text("", encoding="utf-8")
    assert [p.name for p in recording_files(tmp_path, "r_data_1")] == [
        "r_data_1.csv", "r_data_1.meta.json", "r_data_1_sensors.csv",
    ]


def test_data_saving_publishes_the_recording_name(tmp_path) -> None:
    ctx = _context(tmp_path)
    calls: list[tuple] = []
    ctx.mqtt_service.publish_data_saving = lambda enabled, filename=None: calls.append((enabled, filename))  # type: ignore[method-assign]
    name = ctx.set_data_saving(True)
    ctx.set_data_saving(False)
    assert calls == [(True, name), (False, None)]


@pytest.fixture
def client(tmp_path):
    ctx = _context(tmp_path)
    (tmp_path / "secret.yaml").write_text(CONFIG, encoding="utf-8")
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(config_router.router)
    return TestClient(app), ctx


@pytest.mark.parametrize("name", ["../secret.yaml", "..\\secret.yaml", "sub/system.yaml", "system.txt"])
def test_load_and_download_reject_paths_outside_config_dir(client, name) -> None:
    http, ctx = client
    assert http.post("/api/config/load", params={"path": name}).status_code == 400
    assert ctx.config_service.config_path.name == "system.yaml"


def test_load_records_who_changed_the_config(client, tmp_path) -> None:
    http, ctx = client
    (ctx.config_service.config_path.parent / "launch.yaml").write_text(CONFIG, encoding="utf-8")
    resp = http.post("/api/config/load", params={"path": "launch.yaml"}, headers={"x-client-id": "c9"})
    assert resp.status_code == 200
    last = _history(tmp_path)[-1]
    assert last["file"] == "launch.yaml" and last["source"] == "load"
    assert last["client"].startswith("c9 (")
