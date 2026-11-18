"""Fixtures that run real processes: a Mosquitto broker and tools/novaMock.py.

Mosquitto must be installed. Without it these tests are skipped, unless
NOVA_REQUIRE_INTEGRATION is set (as it is in CI), in which case they fail.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NOVA_MOCK = REPO_ROOT / "tools" / "novaMock.py"

# Where the Windows installer puts it; it isn't on PATH by default.
_WINDOWS_MOSQUITTO = Path(r"C:\Program Files\mosquitto\mosquitto.exe")


def _find_mosquitto() -> str | None:
    found = shutil.which("mosquitto")
    if found:
        return found
    for candidate in (_WINDOWS_MOSQUITTO, Path("/usr/sbin/mosquitto")):
        if candidate.is_file():
            return str(candidate)
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, proc: subprocess.Popen, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"mosquitto exited early with code {proc.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"mosquitto did not open port {port} within {timeout_s}s")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture(scope="session")
def mqtt_broker(tmp_path_factory) -> tuple[str, int]:
    """A private Mosquitto on a free localhost port. Yields (host, port)."""
    exe = _find_mosquitto()
    if exe is None:
        message = "Mosquitto is not installed"
        if os.getenv("NOVA_REQUIRE_INTEGRATION"):
            pytest.fail(message)
        pytest.skip(message)

    port = _free_port()
    conf = tmp_path_factory.mktemp("mosquitto") / "mosquitto.conf"
    conf.write_text(f"listener {port} 127.0.0.1\nallow_anonymous true\n", encoding="utf-8")

    proc = subprocess.Popen(
        [exe, "-c", str(conf)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port(port, proc, timeout_s=10)
        yield "127.0.0.1", port
    finally:
        _stop(proc)


@pytest.fixture
def nova_mock(mqtt_broker) -> subprocess.Popen:
    """tools/novaMock.py publishing simulated telemetry to the test broker."""
    host, port = mqtt_broker
    proc = subprocess.Popen(
        [sys.executable, str(NOVA_MOCK), "--broker", host, "--port", str(port), "--hz", "20"],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield proc
    finally:
        _stop(proc)
