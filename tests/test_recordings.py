"""Recording start/stop over REST and data-file handling."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.context import AppContext
from app.routers import data as data_router
from app.routers import flags as flags_router
from app.services.recording_service import META_SUFFIX, recording_files

CONFIG = "Sensors: []\nActuators: []\n"


@pytest.fixture
def station(tmp_path):
    path = tmp_path / "system.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    ctx = AppContext(path, tmp_path / "data")
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    ctx.config_service.record_active("startup")
    published: list[tuple[bool, str | None]] = []
    ctx.mqtt_service.publish_data_saving = lambda enabled, filename=None: published.append((enabled, filename))  # type: ignore[method-assign]
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(flags_router.router)
    app.include_router(data_router.router)
    return TestClient(app), ctx, published


def _meta(ctx: AppContext, name: str) -> dict:
    return json.loads((ctx.data_dir / f"{name}{META_SUFFIX}").read_text(encoding="utf-8"))


def test_start_and_stop_over_rest(station) -> None:
    http, ctx, published = station

    assert http.post("/api/flags/data-saving", json={"enabled": True}).status_code == 200
    name = ctx.recordings.active
    assert name and name.endswith("_data_0")
    assert published == [(True, name)]
    assert ctx.runtime.data_saving_enabled is True
    assert _meta(ctx, name)["stopped"] is None
    assert http.get("/api/flags/data-saving").json() == {"enabled": True}

    assert http.post("/api/flags/data-saving", json={"enabled": False}).status_code == 200
    assert ctx.recordings.active is None
    assert published[-1] == (False, None)
    meta = _meta(ctx, name)
    assert meta["stopped"] is not None
    assert [entry["source"] for entry in meta["config"]] == ["start"]
    assert meta["config"][0]["file"] == "system.yaml"
    assert len(meta["config"][0]["hash"]) == 16


def test_stop_without_start_is_harmless(station) -> None:
    http, ctx, published = station
    assert http.post("/api/flags/data-saving", json={"enabled": False}).status_code == 200
    assert not list(ctx.data_dir.glob(f"*{META_SUFFIX}"))


def test_restart_gets_a_fresh_name_and_closes_the_old_meta(station) -> None:
    http, ctx, _ = station
    http.post("/api/flags/data-saving", json={"enabled": True})
    first = ctx.recordings.active
    http.post("/api/flags/data-saving", json={"enabled": True})
    second = ctx.recordings.active

    assert first != second
    assert _meta(ctx, first)["stopped"] is not None
    assert _meta(ctx, second)["stopped"] is None


def test_logger_files_are_grouped_with_their_recording(station) -> None:
    http, ctx, _ = station
    http.post("/api/flags/data-saving", json={"enabled": True})
    name = ctx.recordings.active
    for file in (f"FAS_{name}.csv", f"novaGround_{name}_sensors.csv", f"novaGround_{name}_actuators.csv"):
        (ctx.data_dir / file).write_text("t,v\n", encoding="utf-8")
    (ctx.data_dir / "unrelated.csv").write_text("x\n", encoding="utf-8")

    names = {p.name for p in recording_files(ctx.data_dir, name)}

    assert names == {
        f"FAS_{name}.csv",
        f"novaGround_{name}_sensors.csv",
        f"novaGround_{name}_actuators.csv",
        f"{name}{META_SUFFIX}",
    }


# --- data files ---

def test_upload_list_and_download_roundtrip(station) -> None:
    http, ctx, _ = station
    body = b"time,value\n1,2\n"

    response = http.post("/api/data-files/upload", files={"file": ("run1.csv", body, "text/csv")})

    assert response.json() == {"file_name": "run1.csv", "bytes_written": len(body)}
    assert http.get("/api/data-files").json() == ["run1.csv"]
    assert http.get("/api/data-files/run1.csv").content == body


def test_upload_requires_a_name(station) -> None:
    http, *_ = station
    response = http.post("/api/data-files/upload", files={"file": ("", b"x", "text/csv")})
    assert response.status_code in (400, 422)


@pytest.mark.parametrize(
    "uploaded",
    ["../escape.csv", "..\escape.csv", "C:\Windows\escape.csv", "/etc/escape.csv", "sub/dir/escape.csv"],
)
def test_upload_never_writes_outside_the_data_dir(station, tmp_path, uploaded) -> None:
    http, ctx, _ = station
    before = {p for p in tmp_path.rglob("*") if p.is_file()}

    http.post("/api/data-files/upload", files={"file": (uploaded, b"x", "text/csv")})

    created = {p for p in tmp_path.rglob("*") if p.is_file()} - before
    assert all(ctx.data_dir in p.parents for p in created), created


def test_overlong_name_does_not_crash_the_server(station) -> None:
    http, *_ = station
    response = http.post("/api/data-files/upload", files={"file": ("a" * 400 + ".csv", b"x", "text/csv")})
    assert response.status_code == 400


@pytest.mark.parametrize("name", ["missing.csv", "run1.txt", "..%2Fsystem.csv"])
def test_download_rejects_missing_and_non_csv(station, name) -> None:
    http, ctx, _ = station
    (ctx.data_dir / "run1.txt").write_text("x", encoding="utf-8")
    assert http.get(f"/api/data-files/{name}").status_code == 404


def test_download_cannot_escape_the_data_dir(station, tmp_path) -> None:
    http, *_ = station
    (tmp_path / "secret.csv").write_text("secret", encoding="utf-8")
    for name in ("..%2Fsecret.csv", "..%5Csecret.csv", "%2e%2e/secret.csv"):
        response = http.get(f"/api/data-files/{name}")
        assert response.status_code == 404 or response.content != b"secret", name
