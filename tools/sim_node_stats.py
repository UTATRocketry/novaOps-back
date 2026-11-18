"""Runtime stats and actuator state for novaSim, mirroring novaGround.

novaGround (the Pi program) publishes, when started with ``--stats on``:

    nova/stats/<node>/info   retained, on every (re)connect: what the process is
    nova/stats/<node>        every --stats-ms: timing and error counters
    nova/state/<node>        retained, every --stats-ms: what the actuators hold

and, with ``--stop-actuate``, drops every output write while reporting it as done.
This module produces the same messages for novaSim (node ``novaSim``) so the backend
and frontend can be built against them without a Pi. The shapes follow
novaGround's docs/instrumentation.md (schema 1) and docs/bench-safety.md. The
timing distributions use the same fixed log-spaced buckets (16 per octave from 1 us),
so percentiles are within about 4.4% of the true value, clamped to min and max.

Pure Python, stdlib only, no MQTT: novaSim.py does the publishing.
"""

from __future__ import annotations

import math
import threading
import time

NODE = "novaSim"
SCHEMA = 1

BUCKETS_PER_OCTAVE = 16
LOWEST_MS = 0.001
BUCKET_COUNT = 28 * BUCKETS_PER_OCTAVE  # 1 us .. about 4.5 minutes
OVERRUN_PERIOD_FACTOR = 2.0
DEFAULT_STATS_MS = 1000

RELAY_COUNT = 16
SERVO_CHANNELS = 16

_ON = {"on", "1", "true", "yes"}
_OFF = {"off", "0", "false", "no"}


def parse_stats_flag(value) -> bool | None:
    """True/False for a readable on/off value, None for anything else."""
    text = str(value).strip().lower()
    if text in _ON:
        return True
    if text in _OFF:
        return False
    return None


def parse_stats_ms(value) -> int | None:
    """A positive whole number of milliseconds, else None."""
    text = str(value).strip()
    if text.isdigit() and int(text) > 0:
        return int(text)
    return None


class Distribution:
    """Log-bucket histogram of values in ms with exact min and max."""

    def __init__(self) -> None:
        self.count = 0
        self.min = math.inf
        self.max = -math.inf
        self._buckets: dict[int, int] = {}

    def add(self, value_ms: float) -> None:
        self.count += 1
        self.min = min(self.min, value_ms)
        self.max = max(self.max, value_ms)
        if value_ms <= LOWEST_MS:
            index = 0
        else:
            index = min(BUCKET_COUNT - 1, int(math.log2(value_ms / LOWEST_MS) * BUCKETS_PER_OCTAVE) + 1)
        self._buckets[index] = self._buckets.get(index, 0) + 1

    def _percentile(self, fraction: float) -> float:
        target = max(1, math.ceil(self.count * fraction))
        seen = 0
        for index in sorted(self._buckets):
            seen += self._buckets[index]
            if seen >= target:
                upper = LOWEST_MS * 2 ** (index / BUCKETS_PER_OCTAVE)
                return min(max(upper, self.min), self.max)
        return self.max

    def summary(self) -> dict:
        if self.count == 0:
            return {"count": 0, "min": 0.0, "p50": 0.0, "p99": 0.0, "max": 0.0}
        return {
            "count": self.count,
            "min": round(self.min, 4),
            "p50": round(self._percentile(0.50), 4),
            "p99": round(self._percentile(0.99), 4),
            "max": round(self.max, 4),
        }


class Windowed:
    """A distribution over the whole run (`total`) and since the last stats message (`last`)."""

    def __init__(self) -> None:
        self.total = Distribution()
        self.last = Distribution()

    def add(self, value_ms: float) -> None:
        self.total.add(value_ms)
        self.last.add(value_ms)

    def summary_and_reset(self) -> dict:
        out = {"last": self.last.summary(), "total": self.total.summary()}
        self.last = Distribution()
        return out


class NodeStats:
    """What novaGround's stats and state messages hold, for one simulated node."""

    def __init__(self, enabled: bool = False, interval_ms: int = DEFAULT_STATS_MS,
                 stop_actuate: bool = False, loops: dict[str, float] | None = None,
                 node: str = NODE) -> None:
        self.enabled = enabled
        self.interval_ms = interval_ms
        self.stop_actuate = stop_actuate
        self.node = node
        self.loops_configured = dict(loops or {})
        self._lock = threading.Lock()
        self._start_mono = time.monotonic()
        self.started_ms = int(time.time() * 1000)
        self._seq = 0
        self._packet_seq = 0
        self._ticks: dict[str, int] = {n: 0 for n in self.loops_configured}
        self._overruns: dict[str, int] = {n: 0 for n in self.loops_configured}
        self._periods: dict[str, Windowed] = {n: Windowed() for n in self.loops_configured}
        self._commands: dict[str, dict[str, Windowed | int]] = {}
        self.publish_failures = 0
        self.daq_read_errors = 0
        self.actuator_read_errors = 0
        self.stopped_writes = 0
        # Actuators, as the drivers would read them back. novaGround sets every
        # relay on and every servo channel low at startup.
        self._relay_mask = (1 << RELAY_COUNT) - 1
        self._servos: dict[int, dict] = {}
        self._gpios: dict[int, int] = {}

    # -- topics ------------------------------------------------------------
    @property
    def info_topic(self) -> str:
        return f"nova/stats/{self.node}/info"

    @property
    def stats_topic(self) -> str:
        return f"nova/stats/{self.node}"

    @property
    def state_topic(self) -> str:
        return f"nova/state/{self.node}"

    # -- recording ---------------------------------------------------------
    def record_tick(self, loop: str, period_ms: float) -> None:
        """One pass of a loop, `period_ms` after the previous one."""
        if not self.enabled or loop not in self._ticks:
            return
        with self._lock:
            self._ticks[loop] += 1
            self._periods[loop].add(period_ms)
            if period_ms >= OVERRUN_PERIOD_FACTOR * self.loops_configured[loop]:
                self._overruns[loop] += 1

    def record_command(self, ctype: str, handled_ms: float, done_ms: float) -> None:
        """A command that reached its handler: ms from receipt to the handler, and to done."""
        if not self.enabled:
            return
        with self._lock:
            entry = self._commands.setdefault(
                ctype, {"count": 0, "handled": Windowed(), "done": Windowed()})
            entry["count"] += 1
            entry["handled"].add(handled_ms)
            entry["done"].add(done_ms)

    def next_packet_seq(self) -> int:
        with self._lock:
            self._packet_seq += 1
            return self._packet_seq

    # -- actuators (writes are dropped under --stop-actuate) ---------------
    def _write_allowed(self) -> bool:
        if self.stop_actuate:
            with self._lock:
                self.stopped_writes += 1
            return False
        return True

    def apply_relay(self, relay_id: int, state: int) -> None:
        if not 0 <= relay_id < RELAY_COUNT or not self._write_allowed():
            return
        with self._lock:
            if state:
                self._relay_mask |= 1 << relay_id
            else:
                self._relay_mask &= ~(1 << relay_id)

    def apply_servo(self, channel: int, angle) -> None:
        """`angle` is a pulse width in us, or 'on' / 'off'."""
        if not 0 <= channel < SERVO_CHANNELS or not self._write_allowed():
            return
        text = str(angle).strip().lower()
        with self._lock:
            if text in ("on", "off"):
                self._servos[channel] = {"id": channel, "output": text}
            else:
                try:
                    pulse = int(float(text))
                except ValueError:
                    return
                self._servos[channel] = {"id": channel, "output": "pulse", "pulse_us": pulse}

    def apply_gpio(self, pin_id: int, state: int) -> None:
        if not self._write_allowed():
            return
        with self._lock:
            self._gpios[pin_id] = 1 if state else 0

    # -- messages ----------------------------------------------------------
    def info(self) -> dict:
        return {
            "schema": SCHEMA,
            "build_id": self.node,
            "started_ms": self.started_ms,
            "stats_interval_ms": self.interval_ms,
            "actuation": "stopped" if self.stop_actuate else "enabled",
            "loops": {name: {"configured_ms": ms} for name, ms in sorted(self.loops_configured.items())},
            "measurement": {
                "overrun_period_factor": OVERRUN_PERIOD_FACTOR,
                "buckets_per_octave": BUCKETS_PER_OCTAVE,
                "lowest_ms": LOWEST_MS,
                "relative_error": 0.0443,
            },
        }

    def stats(self) -> dict:
        """The next stats message. Starts a new `last` window."""
        with self._lock:
            self._seq += 1
            loops = {}
            for name in sorted(self._ticks):
                loops[name] = {
                    "ticks": self._ticks[name],
                    "overruns": self._overruns[name],
                    "period_ms": self._periods[name].summary_and_reset(),
                }
            commands = {}
            for ctype in sorted(self._commands):
                entry = self._commands[ctype]
                commands[ctype] = {
                    "count": entry["count"],
                    "received_to_handled_ms": entry["handled"].summary_and_reset(),
                    "received_to_done_ms": entry["done"].summary_and_reset(),
                }
            return {
                "seq": self._seq,
                "time_ms": int(time.time() * 1000),
                "uptime_s": round(time.monotonic() - self._start_mono, 1),
                "loops": loops,
                "commands": commands,
                "publish_failures": self.publish_failures,
                "daq_read_errors": self.daq_read_errors,
                "actuator_read_errors": self.actuator_read_errors,
                "stopped_writes": self.stopped_writes,
            }

    def state(self) -> dict:
        """What the actuators hold, not what was commanded (under --stop-actuate they differ)."""
        with self._lock:
            channels = [
                dict(self._servos.get(ch, {"id": ch, "output": "off"}))
                for ch in range(SERVO_CHANNELS)
            ]
            return {
                "schema": SCHEMA,
                "time_ms": int(time.time() * 1000),
                "relays": {"ok": True, "mask": self._relay_mask},
                "servos": {"ok": True, "channels": channels},
                "gpios": {"ok": True, "outputs": [
                    {"pin_id": pin, "state": level} for pin, level in sorted(self._gpios.items())]},
            }
