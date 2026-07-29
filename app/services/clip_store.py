"""Temp-directory staging area for soundboard clips awaiting bridge pickup.

Transcoded clips used to ride the MQTT command base64-encoded, which capped them
at a few hundred KB (broker message limits, and the whole clip sat in every
subscriber's memory). Instead the backend now writes the clip to a temp file,
hands the bridge a one-shot download URL, and the bridge fetches it over HTTP.

Tokens are unguessable (``secrets.token_urlsafe``) and short-lived: a clip is
dropped as soon as the bridge acknowledges the upload, and any clip nobody
collects is purged after ``ttl_s``. Nothing here survives a restart — the temp
directory is recreated empty, so a stale URL simply 404s.
"""
from __future__ import annotations

import logging
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

LOGGER = logging.getLogger(__name__)

# A clip nobody downloads is garbage after this long; the bridge normally picks
# it up within a second of the command being published.
DEFAULT_TTL_S = 900.0


@dataclass
class StagedClip:
    token: str
    path: Path
    size: int
    created: float = field(default_factory=time.monotonic)
    meta: dict = field(default_factory=dict)


class ClipStore:
    """Thread-safe store of staged clip files in a private temp directory."""

    def __init__(self, ttl_s: float = DEFAULT_TTL_S, root: Path | None = None) -> None:
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        self._clips: dict[str, StagedClip] = {}
        if root is None:
            self._root = Path(tempfile.mkdtemp(prefix="novaops-clips-"))
        else:
            self._root = Path(root)
            self._root.mkdir(parents=True, exist_ok=True)
        LOGGER.info("Clip staging directory: %s", self._root)

    @property
    def root(self) -> Path:
        return self._root

    def stage(self, data: bytes, meta: dict | None = None) -> StagedClip:
        """Write `data` to a fresh temp file and return its staging record."""
        self.purge_expired()
        token = secrets.token_urlsafe(24)
        path = self._root / f"{token}.clip"
        path.write_bytes(data)
        clip = StagedClip(token=token, path=path, size=len(data), meta=dict(meta or {}))
        with self._lock:
            self._clips[token] = clip
        LOGGER.info("Staged clip token=%s bytes=%d path=%s", token, clip.size, path)
        return clip

    def get(self, token: str) -> StagedClip | None:
        self.purge_expired()
        with self._lock:
            clip = self._clips.get(token)
        if clip is None or not clip.path.exists():
            return None
        return clip

    def discard(self, token: str) -> None:
        """Drop a staged clip and delete its file. Safe to call twice."""
        with self._lock:
            clip = self._clips.pop(token, None)
        if clip is None:
            return
        try:
            clip.path.unlink(missing_ok=True)
        except OSError as exc:  # noqa: BLE001
            LOGGER.warning("Failed to remove staged clip %s: %s", clip.path, exc)

    def purge_expired(self) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [t for t, c in self._clips.items() if now - c.created > self._ttl_s]
        for token in expired:
            LOGGER.info("Purging expired staged clip token=%s", token)
            self.discard(token)
