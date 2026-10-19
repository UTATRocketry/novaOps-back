"""Recording metadata: which config (and software) produced each data file.

The loggers (novaGround on the Pi, fas_bridge on the PC) write raw values, so
a CSV is only interpretable alongside the calibration that was active when it
was taken. When a recording starts, this service picks its name and writes
<name>.meta.json into the data directory:

  {
    "recording": "2026-10-04-14_data_0",
    "env": "prod", "release": "v2026.10.04", "backend_commit": "92fb376...",
    "started": "...", "stopped": "...",
    "config": [ {"file": "system.yaml", "hash": "...", "at": "...", "source": "start"},
                ... one more entry per config change made DURING the recording ]
  }

The data files themselves are named by the loggers from the same name:
<name>.csv (fas_bridge), <name>_sensors.csv and <name>_actuators.csv
(novaGround). ops/Nova.ps1 `archive` bundles all of them with the referenced
config snapshots.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services.config_history import utc_now, write_atomic

LOGGER = logging.getLogger(__name__)

META_SUFFIX = ".meta.json"
META_FORMAT = 1


def recording_files(data_dir: Path, name: str) -> list[Path]:
    """Every file belonging to recording `name`.

    Matches <name>.<ext> and <name>_<word>.<ext> but not <name>0... so that
    "..._data_1" never claims "..._data_10_sensors.csv".
    """
    pattern = re.compile(rf"^{re.escape(name)}(\.|_[A-Za-z])")
    return sorted(p for p in data_dir.iterdir() if p.is_file() and pattern.match(p.name))


class RecordingService:
    def __init__(
        self,
        data_dir: Path,
        env: str = "",
        software: dict[str, Any] | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._env = env
        self._software = software or {}
        self._lock = threading.Lock()
        self._active: str | None = None
        self._meta: dict[str, Any] | None = None
        # Most recent (file, hash) of the active config, kept up to date by
        # config_changed() so a recording can say what it started with.
        self._current_config: dict[str, Any] | None = None

    @property
    def active(self) -> str | None:
        return self._active

    def meta_path(self, name: str) -> Path:
        return self._data_dir / f"{name}{META_SUFFIX}"

    def config_changed(self, file_name: str, digest: str | None, source: str) -> None:
        """ConfigService listener: called after every load/edit/upload/startup."""
        entry = {"file": file_name, "hash": digest, "at": utc_now(), "source": source}
        with self._lock:
            self._current_config = entry
            if self._meta is not None:
                # A calibration changed mid-recording: the data from here on was
                # taken with a different config, and the record must say so.
                self._meta["config"].append(entry)
                self._write_meta()

    def start(self) -> str:
        with self._lock:
            if self._meta is not None:
                self._finish_locked()
            name = self._unique_name()
            config = dict(self._current_config) if self._current_config else None
            if config is not None:
                config["source"] = "start"
            self._active = name
            self._meta = {
                "format": META_FORMAT,
                "recording": name,
                "env": self._env,
                **self._software,
                "started": utc_now(),
                "stopped": None,
                "config": [config] if config else [],
            }
            self._write_meta()
            LOGGER.info("Recording %s started", name)
            return name

    def stop(self) -> str | None:
        with self._lock:
            return self._finish_locked()

    def _finish_locked(self) -> str | None:
        if self._meta is None:
            return None
        name = self._active
        self._meta["stopped"] = utc_now()
        self._write_meta()
        LOGGER.info("Recording %s stopped", name)
        self._meta = None
        self._active = None
        return name

    def _unique_name(self) -> str:
        # Same naming scheme the loggers have always received, but the counter
        # skips names already on disk, so a backend restart within the hour can
        # no longer reuse (and overwrite) an earlier recording's files.
        prefix = datetime.now().strftime("%Y-%m-%d-%H") + "_data_"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        n = 0
        while self.meta_path(f"{prefix}{n}").exists() or recording_files(self._data_dir, f"{prefix}{n}"):
            n += 1
        return f"{prefix}{n}"

    def _write_meta(self) -> None:
        if self._meta is None or self._active is None:
            return
        try:
            write_atomic(self.meta_path(self._active), json.dumps(self._meta, indent=2).encode("utf-8"))
        except OSError as exc:
            LOGGER.error("Could not write recording metadata for %s: %s", self._active, exc)
