"""Fixtures that run real processes: a Mosquitto broker and tools/novaSim.py.

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
NOVA_SIM = REPO_ROOT / "tools" / "novaSim.py"

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


def _broker_output(log: Path) -> str:
    try:
        text = log.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "(no output captured)"
    return text or "(no output)"


def _start_broker(exe: str, port: int, log: Path) -> subprocess.Popen:
    """Start Mosquitto on `port`, appending its output to `log`.

    No config file: Mosquitto 2.x run with only -p listens on loopback alone and
    allows anonymous clients, which is what these tests need. A config file under
    the pytest temp dir is not an option, since Ubuntu's mosquitto can't open one
    there ("Unable to open config file"). The log is opened here, by Python.
    """
    with log.open("ab") as out:
        return subprocess.Popen([exe, "-p", str(port)], stdout=out, stderr=subprocess.STDOUT)


def _wait_for_port(port: int, proc: subprocess.Popen, log: Path, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"mosquitto exited early with code {proc.returncode}:\n{_broker_output(log)}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(
        f"mosquitto did not open port {port} within {timeout_s}s:\n{_broker_output(log)}"
    )


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
    log = tmp_path_factory.mktemp("mosquitto") / "mosquitto.log"
    proc = _start_broker(exe, port, log)
    try:
        _wait_for_port(port, proc, log, timeout_s=10)
        yield "127.0.0.1", port
    finally:
        _stop(proc)


@pytest.fixture
def nova_sim(mqtt_broker) -> subprocess.Popen:
    """tools/novaSim.py publishing simulated telemetry to the test broker."""
    host, port = mqtt_broker
    proc = subprocess.Popen(
        [sys.executable, str(NOVA_SIM), "--broker", host, "--port", str(port), "--hz", "20"],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield proc
    finally:
        _stop(proc)


class BrokerControl:
    """A private Mosquitto that tests can kill and bring back on the same port."""

    def __init__(self, exe: str, port: int, log: Path) -> None:
        self.host = "127.0.0.1"
        self.port = port
        self._exe = exe
        self._log = log
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        self._proc = _start_broker(self._exe, self.port, self._log)
        _wait_for_port(self.port, self._proc, self._log, timeout_s=10)

    def stop(self) -> None:
        if self._proc is not None:
            _stop(self._proc)
            self._proc = None


@pytest.fixture
def broker_control(tmp_path) -> BrokerControl:
    exe = _find_mosquitto()
    if exe is None:
        message = "Mosquitto is not installed"
        if os.getenv("NOVA_REQUIRE_INTEGRATION"):
            pytest.fail(message)
        pytest.skip(message)

    broker = BrokerControl(exe, _free_port(), tmp_path / "mosquitto.log")
    broker.start()
    try:
        yield broker
    finally:
        broker.stop()
