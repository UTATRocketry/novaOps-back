"""Automatic, append-only history of every config the backend has run with.

Configs are edited from the web interface, often, by people who should never
be making commits - and many saves are half-finished edits made so a page
reload does not lose them. None of that belongs in git. Instead:

  snapshots/<hash>.yaml   every distinct config content, stored once
  history.jsonl           one line per save/load/startup: when, which file,
                          which hash, how it happened and who did it

Recordings refer to configs by hash (see recording_service.py), so the exact
calibration behind any data file can always be recovered, even after the live
file has been edited again.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

LOGGER = logging.getLogger(__name__)

# 16 hex chars (64 bits) is far beyond collision range for a station's configs
# and short enough to read aloud or type into a filename.
HASH_CHARS = 16


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:HASH_CHARS]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_atomic(path: Path, data: bytes) -> None:
    """Write via a temp file + rename so a crash never leaves a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class ConfigHistory:
    def __init__(self, history_dir: Path | None, env: str = "") -> None:
        # history_dir=None still hashes (so recordings can name their config)
        # but stores nothing; used by tests and ad-hoc runs.
        self._dir = history_dir
        self._env = env
        self._lock = threading.Lock()
        self._last_logged: dict[str, str] = {}

    @property
    def history_dir(self) -> Path | None:
        return self._dir

    def snapshot_path(self, digest: str) -> Path | None:
        if self._dir is None:
            return None
        return self._dir / "snapshots" / f"{digest}.yaml"

    def snapshot(self, path: Path, source: str, client: str | None = None) -> str | None:
        """Store the file's current content (once) and log the event.

        Returns the content hash, or None if the file does not exist. Never
        raises: failing to write history must not fail a config save.
        """
        try:
            data = path.read_bytes()
        except OSError:
            return None
        digest = content_hash(data)
        if self._dir is None:
            return digest

        try:
            with self._lock:
                snap = self.snapshot_path(digest)
                if snap is not None and not snap.exists():
                    write_atomic(snap, data)

                # Repeated saves of identical content (autosave, double click)
                # add nothing to the record, so they are not logged again.
                if self._last_logged.get(path.name) == digest and source == "edit":
                    return digest
                self._last_logged[path.name] = digest

                entry = {
                    "time": utc_now(),
                    "env": self._env,
                    "file": path.name,
                    "hash": digest,
                    "source": source,
                    "client": client,
                }
                self._dir.mkdir(parents=True, exist_ok=True)
                with (self._dir / "history.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry) + "\n")
        except OSError as exc:
            LOGGER.error("Could not record config history for %s: %s", path, exc)
        return digest
