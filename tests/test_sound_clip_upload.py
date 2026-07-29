"""Soundboard clip upload: clips are staged for HTTP pickup, not sent over MQTT.

Covers the staging store, the download endpoint, and the request/ack handshake
that makes /fas/sound/upload wait for the bridge to confirm the upload.
"""
import asyncio
import zlib

import pytest

from app.context import AppContext
from app.services.clip_store import ClipStore

CONFIG = """
Sensors: []
Actuators: []
"""


def _context(tmp_path) -> AppContext:
    config_path = tmp_path / "system.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    return AppContext(config_path, tmp_path / "data")


def test_clip_store_stage_get_discard(tmp_path) -> None:
    store = ClipStore(root=tmp_path / "clips")
    clip = store.stage(b"hello", meta={"name": "chime"})

    fetched = store.get(clip.token)
    assert fetched is not None
    assert fetched.path.read_bytes() == b"hello"
    assert fetched.size == 5
    assert fetched.meta["name"] == "chime"

    store.discard(clip.token)
    assert store.get(clip.token) is None
    assert not clip.path.exists()
    store.discard(clip.token)  # idempotent


def test_clip_store_purges_expired(tmp_path) -> None:
    store = ClipStore(ttl_s=0.0, root=tmp_path / "clips")
    clip = store.stage(b"data")
    store.purge_expired()
    assert store.get(clip.token) is None
    assert not clip.path.exists()


def test_upload_command_carries_url_not_clip_bytes(tmp_path) -> None:
    """The MQTT command must reference the staged clip, never inline the bytes —
    that inline base64 was what limited clip size."""
    ctx = _context(tmp_path)
    published: list[dict] = []
    ctx.mqtt_service.publish_device_commands = published.extend  # type: ignore[method-assign]

    clip = bytes(range(256)) * 8       # 2 KB, larger than any MQTT-friendly cap
    crc = zlib.crc32(clip) & 0xFFFFFFFF

    async def scenario() -> dict:
        task = asyncio.create_task(ctx.command_service.apply_fas_sound_upload(
            name="siren", clip=clip, fmt=2, crc32=crc, sample_rate=31250,
            base_url="http://ops.local:8000/", timeout_s=5.0,
        ))
        # Let the command publish before answering it.
        for _ in range(50):
            await asyncio.sleep(0.01)
            if published:
                break
        command = published[0]
        assert "data_b64" not in command
        token = command["upload_id"]
        assert command["url"] == f"http://ops.local:8000/api/fas/sound/clip/{token}"
        assert command["path"] == f"/api/fas/sound/clip/{token}"
        assert command["bytes"] == len(clip)
        assert command["crc32"] == crc
        # The staged file holds exactly the clip the bridge will download.
        staged = ctx.clip_store.get(token)
        assert staged is not None and staged.path.read_bytes() == clip

        # Bridge acks on the console topic, as fas_bridge does.
        ctx._on_console_message({"type": "sound_upload_result", "upload_id": token,
                                 "ok": True, "stage": "done", "clip_count": 3})
        return await task

    result = asyncio.run(scenario())
    assert result["ok"] is True
    assert result["clip_count"] == 3
    assert result["bytes"] == len(clip)
    # Acked clips are dropped from the staging dir.
    assert ctx.clip_store.get(result["upload_id"]) is None


def test_upload_reports_bridge_failure(tmp_path) -> None:
    ctx = _context(tmp_path)
    published: list[dict] = []
    ctx.mqtt_service.publish_device_commands = published.extend  # type: ignore[method-assign]

    async def scenario() -> dict:
        task = asyncio.create_task(ctx.command_service.apply_fas_sound_upload(
            name="siren", clip=b"\x00" * 64, fmt=2, crc32=0, sample_rate=31250,
            timeout_s=5.0,
        ))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if published:
                break
        ctx._on_console_message({
            "type": "sound_upload_result", "upload_id": published[0]["upload_id"],
            "ok": False, "stage": "verify", "error": "crc mismatch"})
        return await task

    result = asyncio.run(scenario())
    assert result["ok"] is False
    assert result["stage"] == "verify"
    assert result["error"] == "crc mismatch"


def test_upload_times_out_without_ack(tmp_path) -> None:
    ctx = _context(tmp_path)
    ctx.mqtt_service.publish_device_commands = lambda commands: None  # type: ignore[method-assign]

    result = asyncio.run(ctx.command_service.apply_fas_sound_upload(
        name="siren", clip=b"\x00" * 32, fmt=2, crc32=0, sample_rate=31250,
        timeout_s=0.05,
    ))
    assert result["ok"] is False
    assert result["stage"] == "timeout"
    # Left staged so a slow bridge can still collect it; the TTL cleans up.
    assert ctx.clip_store.get(result["upload_id"]) is not None


def test_clip_download_endpoint(tmp_path) -> None:
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from app.deps import get_context
    from app.routers import commands as commands_router
    from fastapi import FastAPI

    ctx = _context(tmp_path)
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(commands_router.router)
    app.dependency_overrides[get_context] = lambda: ctx

    clip = b"\x01\x02\x03\x04" * 100
    staged = ctx.clip_store.stage(clip, meta={"name": "chime", "crc32": 42})

    with fastapi_testclient.TestClient(app) as client:
        resp = client.get(f"/api/fas/sound/clip/{staged.token}")
        assert resp.status_code == 200
        assert resp.content == clip
        assert resp.headers["x-clip-bytes"] == str(len(clip))
        assert resp.headers["x-clip-crc32"] == "42"

        assert client.get("/api/fas/sound/clip/nope").status_code == 404
