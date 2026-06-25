"""
novaGround + FAS-bridge simulator (no hardware, no serial).

Stands in for the two programs that feed the novaOps backend over MQTT so the
backend and frontend can be exercised end to end without a wire:

  * novaGround   — GCS engine sensors + relay/servo/gpio/data_file commands.
  * fas_bridge   — FAS engine sensors, the nested flight snapshot, and the
                   fas / console / data_file command surface.

It mirrors the *current* MQTT contract of ``tools/fas_bridge.py`` and
``tools/novaGround_dummy.py`` (not the older flat ``data``+``events`` flight
shape). See ``docs/FLIGHT_INTERFACE.md`` and ``INTERFACES.md`` for the wire
formats.

Publishes:
    nova/telemetry/engine  — two messages per tick:
        * GCS  packet: source="novaGround", sensors is a LIST keyed by
                       hat_id / channel_id.
        * FAS  packet: source="FAS", sensors is a DICT keyed "node:channel"
                       (e.g. "EPB_0:1"), each {node, channel, value, timestamp},
                       exactly like fas_bridge's persistent engine dict.
    nova/telemetry/flight  — source="FAS", a single nested snapshot under
                       "data": fas_boards / fas_actuators / fas_sensors /
                       fas_board_status / fas_fmc / fas_pmb / fas_imc / fas_fsm.
    nova/console           — console responses, and (while console mode is on)
                       a mocked "fas_frame" RX stream.

Listens on nova/command and nova/control:
    relay / servo / gpio   — GCS device commands (novaGround side).
    type "fas"             — FAS device commands, both the high-level
                             port/action shape and the direct op shape.
    type "console"         — start/stop/list_ports/configure/tx.
    type "data_file"       — data-saving start/stop (on either topic).
    novaLock lockout       — logged from nova/control.

Note: this sim does NOT emulate the removed legacy "fas_cmd" system commands or
the flight "events" array; the live fas_bridge no longer uses them.

Optional control UI:
    Pass ``--ui`` to start a small browser control panel (stdlib http.server,
    no extra dependencies) at http://localhost:8765. From it you can switch each
    sensor's waveform (flat / sine / square / triangle / sawtooth / s-curve /
    ramp / noise), change the flight state, start/stop a fake SD-card fill, and
    watch every received command with a live format validation. Without
    ``--ui`` novaMock runs exactly as before, fully headless.

Usage:
    python tools/novaMock.py                 # headless
    python tools/novaMock.py --ui            # with control panel on :8765
    python tools/novaMock.py --ui --ui-port 9000

Environment variables (CLI flags override these):
    NOVA_MQTT_BROKER   MQTT broker host (default: localhost)
    NOVA_MQTT_PORT     MQTT broker port (default: 1883)
    NOVA_PUBLISH_HZ    Engine-sensor publish rate in Hz (default: 20)
    NOVA_FLIGHT_HZ     Flight telemetry publish rate in Hz (default: 4)
"""

import argparse
import bisect
import json
import math
import os
import random
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import paho.mqtt.client as mqtt

BROKER = os.getenv("NOVA_MQTT_BROKER", "localhost")
PORT = int(os.getenv("NOVA_MQTT_PORT", "1883"))
HZ = float(os.getenv("NOVA_PUBLISH_HZ", "20"))
FLIGHT_HZ = float(os.getenv("NOVA_FLIGHT_HZ", "4"))

TELEMETRY_TOPIC = "nova/telemetry/engine"
FLIGHT_TOPIC = "nova/telemetry/flight"
CONSOLE_TOPIC = "nova/console"
COMMAND_TOPIC = "nova/command"
CONTROL_TOPIC = "nova/control"
CLIENT_ID = "novaMock"

# Coarse lock guarding every piece of mutable sim state below, since the UI
# thread, the MQTT callback thread and the publish loop all touch it.
_lock = threading.RLock()

# ---------------------------------------------------------------------------
# Sensor model. One entry per sensor, each carrying its MQTT addressing and a
# runtime-tunable waveform. "group" selects which packet it ships in:
#   gcs → source="novaGround", keyed hat_id/channel_id (LIST)
#   fas → source="FAS",        keyed node/channel       (DICT)
# Defaults are chosen so headless output stays in each sensor's raw range.
# ---------------------------------------------------------------------------
WAVES = ["flat", "sine", "square", "triangle", "sawtooth", "s_curve", "ramp", "noise"]


def _sensor(group, label, wave="sine", lo=0.0, hi=1.0, period=20.0,
            noise=0.0, value=None, **addr):
    cfg = {"group": group, "label": label, "wave": wave,
           "min": lo, "max": hi, "period": period, "noise": noise,
           "value": value if value is not None else (lo + hi) / 2.0}
    cfg.update(addr)
    return label, cfg


SENSORS = dict([
    # GCS pressure transducers / load cells (raw volts).
    _sensor("gcs", "PGSO", "sine", 0.99, 3.99, 20, 0.02, hat_id=0, channel_id=0),
    _sensor("gcs", "PGS",  "sine", 0.99, 3.99, 20, 0.02, hat_id=0, channel_id=1),
    _sensor("gcs", "PVO",  "sine", 1.98, 7.98, 25, 0.02, hat_id=0, channel_id=4),
    _sensor("gcs", "MOT",  "sine", 3.59, 5.20, 30, 0.05, hat_id=0, channel_id=6),
    _sensor("gcs", "MFT",  "sine", 3.59, 5.20, 30, 0.05, hat_id=0, channel_id=7),
    _sensor("gcs", "CC-LC", "sine", 1.925, 2.06, 18, 0.01, hat_id=1, channel_id=1),
    # FAS EPB-backed transducers (scaled volts, ~0.17-0.67 over 0-900 psi).
    _sensor("fas", "PCC", "sine", 0.2, 0.9, 25, 0.005, node="EPB_0", channel=1),
    _sensor("fas", "POT", "sine", 0.2, 0.9, 25, 0.005, node="EPB_2", channel=0),
    _sensor("fas", "PFT", "sine", 0.2, 0.9, 25, 0.005, node="EPB_4", channel=1),
])

# Flight states (Flight_State_t / lookupTableFlightState in flight.h).
FLIGHT_STATES = [
    "INIT", "STANDBY", "ARMED", "POWERED_ASCENT",
    "COASTING", "APOGEE", "DESCENT", "LANDED",
]

# ---------------------------------------------------------------------------
# Mutable sim state
# ---------------------------------------------------------------------------
_boot_time = time.time()
_flight_state = "STANDBY"
_imc_armed = False
_imc_board_id = 0
_console_active = False
# Per-EPB actuator state, keyed board_id → {channel_idx → state dict}, updated
# as relay/servo/op commands come in so the flight snapshot reflects them.
_epb_actuators: dict[int, dict[int, dict]] = {}
# SD card fill simulation, surfaced in the FMC "sd" snapshot.
_sd = {"filling": False, "pct": 12.0, "total_mb": 32000, "rate": 1.0,
       "last_tick": time.time()}
# Ring buffer of received commands, with a format-validation verdict for the UI.
_recent: deque = deque(maxlen=300)
_cmd_seq = 0

# Flight replay ("Launch"). When active, the flight snapshot is driven by a
# loaded trajectory (an OpenRocket-style CSV or a synthetic profile) instead of
# the manual flight state, and _flight_state is derived from the trajectory.
_sim_csv_default = ""   # set from --sim-csv in main()
_launch = {
    "active": False,
    "t0": 0.0,            # wall-clock start of the replay
    "speed": 1.0,         # playback speed multiplier
    "src": "none",        # description of the loaded source
    "samples": [],        # list of trajectory rows (see load_sim_csv)
    "times": [],          # parallel list of sample times, for bisect
    "apogee_t": 0.0,
    "end_t": 0.0,
}


# ---------------------------------------------------------------------------
# Waveform evaluation
# ---------------------------------------------------------------------------
def _smoothstep(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3.0 - 2.0 * x)


def wave_value(cfg: dict, t: float) -> float:
    w = cfg["wave"]
    lo, hi = cfg["min"], cfg["max"]
    per = max(float(cfg.get("period", 10.0)), 1e-6)
    mid = (lo + hi) / 2.0
    amp = (hi - lo) / 2.0
    phase = (t % per) / per  # 0..1 within the period

    if w == "flat":
        base = float(cfg.get("value", mid))
    elif w == "sine":
        base = mid + amp * math.sin(2.0 * math.pi * phase)
    elif w == "square":
        base = hi if phase < 0.5 else lo
    elif w == "triangle":
        base = lo + (hi - lo) * (2.0 * phase if phase < 0.5 else 2.0 * (1.0 - phase))
    elif w == "sawtooth":
        base = lo + (hi - lo) * phase
    elif w == "s_curve":
        base = lo + (hi - lo) * _smoothstep(phase)
    elif w == "ramp":
        # One-shot ramp from lo to hi over `period`, then holds at hi.
        frac = min((t - _boot_time) / per, 1.0)
        base = lo + (hi - lo) * frac
    elif w == "noise":
        base = random.uniform(lo, hi)
    else:
        base = mid

    n = float(cfg.get("noise", 0.0))
    if n:
        base += random.uniform(-n, n)
    return base


# ---------------------------------------------------------------------------
# Telemetry builders
# ---------------------------------------------------------------------------
def _dumps(payload: dict) -> str:
    """Serialize to JSON, refusing NaN/Infinity. Standard JSON has no such
    literals and browsers' JSON.parse throws on them, which would silently break
    downstream clients — so if any sneaks in we strip it rather than emit it."""
    try:
        return json.dumps(payload, allow_nan=False)
    except ValueError:
        return json.dumps(_strip_nonfinite(payload))


def _strip_nonfinite(obj):
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else 0.0
    if isinstance(obj, dict):
        return {k: _strip_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_nonfinite(v) for v in obj]
    return obj


def build_gcs_packet() -> dict:
    t = time.time()
    timestamp = int(t * 1000)
    with _lock:
        sensors = [
            {"hat_id": c["hat_id"], "channel_id": c["channel_id"],
             "value": round(wave_value(c, t), 4), "timestamp": timestamp}
            for c in SENSORS.values() if c["group"] == "gcs"
        ]
    return {"source": "novaGround", "sensors": sensors}


def build_fas_engine_packet() -> dict:
    t = time.time()
    timestamp = int(t * 1000)
    with _lock:
        sensors = {
            f"{c['node']}:{c['channel']}": {
                "node": c["node"], "channel": c["channel"],
                "value": round(wave_value(c, t), 6), "timestamp": timestamp}
            for c in SENSORS.values() if c["group"] == "fas"
        }
    return {"source": "FAS", "sensors": sensors}


def _board_entry(kind: str, board_id: int, num_channels: int, num_sensors: int) -> dict:
    return {
        "key": f"{kind}:{board_id}",
        "kind": kind,
        "board_id": board_id,
        "online": True,
        "uptime_ms": int((time.time() - _boot_time) * 1000),
        "fw_version": 0x0102,
        "num_channels": num_channels,
        "num_sensors": num_sensors,
        "caps_mask": 0x0F,
    }


def _tick_sd() -> dict:
    """Advance the fake SD fill and return a snapshot of its derived fields."""
    now = time.time()
    with _lock:
        dt = now - _sd["last_tick"]
        _sd["last_tick"] = now
        if _sd["filling"]:
            _sd["pct"] = min(100.0, _sd["pct"] + _sd["rate"] * dt)
        pct = _sd["pct"]
        total = _sd["total_mb"]
        full = pct >= 100.0
        return {
            "state": 4 if (_sd["filling"] and full) else 3,
            "state_name": "error" if full else "logging",
            "err": 0,
            "pct_used": int(round(pct)),
            "free_mb": int(total * (1.0 - pct / 100.0)),
            "total_mb": total,
            "logging": _sd["filling"] and not full,
            "near_full": pct >= 80.0,
            "full": full,
            "rate_reduced": False,
            "stalled": False,
            "rate_div": 1,
        }


# ---------------------------------------------------------------------------
# Flight replay — trajectory loading, synthesis, sampling and state derivation
# ---------------------------------------------------------------------------
# A trajectory "row" is a dict: t, alt(m), vvel(m/s), vacc(m/s^2), lat, lon,
# roll/pitch/yaw(deg/s), temp(degC), pmbar(mbar).
_SIM_FIELDS = ["t", "alt", "vvel", "vacc", "lat", "lon",
               "roll", "pitch", "yaw", "temp", "pmbar"]


def load_sim_csv(path: str) -> list[dict]:
    """Parse an OpenRocket CSV export. Comment/blank lines (starting with '#')
    are skipped; the 11 numeric columns map to _SIM_FIELDS in order. Returns []
    if the file is missing or unparseable."""
    rows: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(",")
                if len(parts) < len(_SIM_FIELDS):
                    continue
                try:
                    # OpenRocket writes "NaN" for columns it can't compute (e.g.
                    # roll/pitch/yaw rates under parachute). float() accepts those,
                    # but a NaN would serialize to invalid JSON and break clients,
                    # so coerce any non-finite value to 0.0 here.
                    vals = [v if math.isfinite(v := float(x)) else 0.0
                            for x in parts[:len(_SIM_FIELDS)]]
                except ValueError:
                    continue
                rows.append(dict(zip(_SIM_FIELDS, vals)))
    except OSError:
        return []
    return rows


def _pressure_mbar_at(alt_m: float) -> float:
    """ISA barometric pressure (mbar) for a given altitude."""
    return 1013.25 * (1.0 - 2.25577e-5 * max(alt_m, 0.0)) ** 5.25588


def generic_profile(dt: float = 0.1) -> list[dict]:
    """Synthesize a plausible single-stage flight when no CSV is available:
    powered ascent, coast to apogee, then a parachute descent to the ground."""
    rows: list[dict] = []
    t, alt, v = 0.0, 0.0, 0.0
    burn, thrust_a = 6.0, 70.0          # s, m/s^2 net upward during burn

    def emit(vacc: float) -> None:
        rows.append({"t": round(t, 3), "alt": max(0.0, alt), "vvel": v,
                     "vacc": vacc, "lat": 32.9901, "lon": -106.9753,
                     "roll": random.uniform(-20, 20), "pitch": random.uniform(-5, 5),
                     "yaw": random.uniform(-5, 5), "temp": 15.0,
                     "pmbar": _pressure_mbar_at(alt)})

    while t < burn:                      # powered ascent
        v += thrust_a * dt; alt += v * dt; t += dt; emit(thrust_a)
    while v > 0:                         # coast to apogee
        a = -9.81 - 0.0008 * v * v; v += a * dt; alt += v * dt; t += dt; emit(a)
    while alt > 0:                       # parachute descent (~ -15 m/s terminal)
        v = -15.0; alt += v * dt; t += dt; emit(-9.81)
    emit(0.0)
    return rows


def _index_trajectory(rows: list[dict]) -> dict:
    """Precompute the time index, apogee time and end time for a trajectory."""
    times = [r["t"] for r in rows]
    apogee_t = max(rows, key=lambda r: r["alt"])["t"] if rows else 0.0
    return {"times": times, "apogee_t": apogee_t,
            "end_t": times[-1] if times else 0.0}


def sample_at(rows: list[dict], times: list[float], t: float) -> dict:
    """Linear-interpolate a trajectory row at playback time t."""
    if not rows:
        return {k: 0.0 for k in _SIM_FIELDS}
    if t <= times[0]:
        return rows[0]
    if t >= times[-1]:
        return rows[-1]
    i = bisect.bisect_right(times, t)
    a, b = rows[i - 1], rows[i]
    span = b["t"] - a["t"]
    f = 0.0 if span <= 0 else (t - a["t"]) / span
    return {k: a[k] + (b[k] - a[k]) * f for k in _SIM_FIELDS}


def _derive_flight_state(s: dict, apogee_t: float, end_t: float) -> str:
    """Map a trajectory sample onto a Flight_State_t value."""
    t = s["t"]
    if t >= end_t - 1e-6 or (t > apogee_t and s["alt"] <= 2.0 and s["vvel"] <= 0.0):
        return "LANDED"
    if abs(t - apogee_t) < 0.3:
        return "APOGEE"
    if t < apogee_t:
        if s["alt"] < 1.0 and s["vvel"] < 1.0:
            return "POWERED_ASCENT"   # on the pad at ignition
        if s["vvel"] > 0.0:
            return "POWERED_ASCENT" if s["vacc"] > 5.0 else "COASTING"
    if t > apogee_t:
        if s["alt"] > 1.0 and s["vvel"] < 0.0:
            return "DESCENT"


def _launch_sample() -> dict | None:
    """If a launch is active, advance it and return the current trajectory
    sample (also updating _flight_state); else None. Caller must hold _lock."""
    global _flight_state
    if not _launch["active"]:
        return None
    elapsed = (time.time() - _launch["t0"]) * _launch["speed"]
    rows, times = _launch["samples"], _launch["times"]
    if elapsed >= _launch["end_t"]:
        _launch["active"] = False
        _flight_state = "LANDED"
        return rows[-1] if rows else None
    s = sample_at(rows, times, elapsed)
    _flight_state = _derive_flight_state(s, _launch["apogee_t"], _launch["end_t"])
    return s


def _fmc_snapshot(t: float, ov: dict | None = None) -> dict:
    uptime = t - _boot_time
    if ov is not None:
        # Driven by a replayed trajectory.
        pressure_pa = ov["pmbar"] * 100.0
        altitude_m = ov["alt"]
        temp_c = ov["temp"]
        lat, lon = ov["lat"], ov["lon"]
        speed_mps = ov["vvel"]
        az_g = ov["vacc"] / 9.80665 + 1.0   # sensed: motion + gravity
        gyro = [ov["roll"], ov["pitch"], ov["yaw"]]
    else:
        ascent = _flight_state in {"POWERED_ASCENT", "COASTING", "APOGEE", "DESCENT"}
        pressure_pa = 101325.0 - (uptime * 12.0 if ascent else 0.0)
        altitude_m = max(0.0, (uptime * 30.0) if ascent else 0.0)
        temp_c = 24.0 + random.uniform(-0.3, 0.3)
        lat = 34.0561 + random.uniform(-1e-4, 1e-4)
        lon = -117.8443 + random.uniform(-1e-4, 1e-4)
        speed_mps = altitude_m / max(uptime, 1.0) if ascent else 0.0
        az_g = (4.0 if _flight_state == "POWERED_ASCENT" else 1.0) + random.uniform(-0.01, 0.01)
        gyro = [random.uniform(-5, 5), random.uniform(-5, 5), random.uniform(-5, 5)]

    return {
        "imu_accel": {
            "unit": "g",
            "axes": [round(random.uniform(-0.02, 0.02), 5),
                     round(random.uniform(-0.02, 0.02), 5),
                     round(az_g, 5)],
        },
        "imu_gyro": {
            "unit": "dps",
            "axes": [round(gyro[0], 4), round(gyro[1], 4), round(gyro[2], 4)],
        },
        "accel_hg": {
            "unit": "g",
            "axes": [round(random.uniform(-0.02, 0.02), 5),
                     round(random.uniform(-0.02, 0.02), 5),
                     round(az_g, 5)],
        },
        "mag": {
            "unit": "uT",
            "axes": [round(30.0 + random.uniform(-0.5, 0.5), 3),
                     round(-5.0 + random.uniform(-0.5, 0.5), 3),
                     round(40.0 + random.uniform(-0.5, 0.5), 3)],
        },
        "baro": {
            "pressure_pa": int(pressure_pa),
            "pressure_hpa": round(pressure_pa / 100.0, 3),
            "temp_c": round(temp_c, 2),
            "altitude_m": round(altitude_m + (0.0 if ov else random.uniform(-1, 1)), 2),
        },
        "gps_pos": {"lat": round(lat, 7), "lon": round(lon, 7)},
        "gps_info": {
            "alt_m": int(altitude_m),
            "fix": 3,
            "sats": random.randint(7, 12),
            "hdop": round(random.uniform(0.8, 1.5), 1),
            "speed_mps": round(speed_mps, 2),
        },
        "health": {"imu_ok": True, "accel_ok": True, "mag_ok": True,
                   "baro_ok": True, "gps_present": True},
        "temp": {"temp_h7": round(31.0 + 4.0 * math.sin(t * 0.1), 2),
                 "temp_pwr": round(36.0 + random.uniform(-0.4, 0.4), 2)},
        "sd": _tick_sd(),
        "radio": {"powered": True, "enabled": True, "every_n": 1,
                  "tx_frames": int(uptime * 10), "tx_bytes": int(uptime * 800)},
    }


def _pmb_snapshot(t: float) -> dict:
    uptime = t - _boot_time
    return {
        "pwr": {"v_8v4": round(8.4 + random.uniform(-0.03, 0.03), 3),
                "i_8v4": round(0.42 + random.uniform(-0.01, 0.01), 3),
                "v_24v0": round(24.0 + random.uniform(-0.05, 0.05), 3),
                "i_24v0": round(0.15 + random.uniform(-0.008, 0.008), 3),
                "p_8v4": round(8.4 * 0.42, 2), "p_24v0": round(24.0 * 0.15, 2)},
        "vmon": {"v_main": round(16.6 - 0.0005 * uptime, 3),
                 "v_batt": round(16.6 - 0.0005 * uptime, 3),
                 "v_gse": round(0.0, 3),
                 "buck_on": True, "boost_on": False, "pg_3v3": True,
                 "pg_8v4": True, "pg_24v0": True, "charger": False,
                 "batt_src": True},
        "temp": {"temp_amb": round(23.0 + random.uniform(-0.5, 0.5), 2),
                 "temp_buck": round(40.0 + random.uniform(-1, 1), 2),
                 "temp_boost": None},
        "charger": {"i_chg_a": 0.0, "v_bat": round(16.6 - 0.0005 * uptime, 3),
                    "present": True, "enabled": False, "vin_good": False,
                    "charging": False, "state": "off", "status": "off",
                    "cells": 4},
    }


def _epb_sensor_status() -> dict:
    return {"connected_mask": 0x03, "saturated_mask": 0x00, "error_mask": 0x00}


def _epb_board_status(board_id: int, t: float) -> dict:
    return {"i_8v4": round(0.5 + 0.2 * abs(math.sin(t * 0.2 + board_id)), 3),
            "i_24v0": round(0.1 + random.uniform(-0.02, 0.02), 3),
            "v_8v4": round(8.4 + random.uniform(-0.03, 0.03), 3),
            "v_24v0": round(24.0 + random.uniform(-0.05, 0.05), 3)}


def build_flight_packet() -> dict:
    t = time.time()

    boards = [
        _board_entry("EPB", 0, 2, 2),
        _board_entry("EPB", 2, 2, 2),
        _board_entry("EPB", 4, 2, 2),
        _board_entry("PMB", 0, 0, 0),
        _board_entry("FMC", 0, 0, 0),
    ]

    with _lock:
        # Advance an in-progress launch first; it updates _flight_state and
        # supplies the trajectory sample that drives the FMC snapshot.
        ov = _launch_sample()
        actuators = {
            f"EPB:{bid}": [by_ch[c] for c in sorted(by_ch)]
            for bid, by_ch in _epb_actuators.items() if by_ch
        }
        imc = {"board_id": _imc_board_id, "armed": _imc_armed,
               "arm_line": _imc_armed, "disarm_line": not _imc_armed, "flags": 0}
        fsm_state = "ARMED" if _imc_armed and _flight_state == "STANDBY" else _flight_state

    data = {
        "fas_boards": boards,
        "fas_actuators": actuators,
        "fas_sensors": {
            "EPB:0": _epb_sensor_status(),
            "EPB:2": _epb_sensor_status(),
            "EPB:4": _epb_sensor_status(),
        },
        "fas_board_status": {
            "EPB:0": _epb_board_status(0, t),
            "EPB:2": _epb_board_status(2, t),
            "EPB:4": _epb_board_status(4, t),
        },
        "fas_fmc": {"FMC:0": _fmc_snapshot(t, ov)},
        "fas_pmb": {"PMB:0": _pmb_snapshot(t)},
        "fas_imc": imc,
        "fas_fsm": {"state": fsm_state},
    }
    return {"source": "FAS", "data": data}


# ---------------------------------------------------------------------------
# Actuator-state bookkeeping (so the flight snapshot reflects commands)
# ---------------------------------------------------------------------------
def _set_actuator(board_id: int, channel: int, **fields) -> None:
    with _lock:
        by_ch = _epb_actuators.setdefault(board_id, {})
        state = by_ch.setdefault(channel, {"channel_idx": channel, "load_sw_on": 0,
                                           "pulse_us": 0, "period_us": 20000,
                                           "fault_bits": 0})
        state.update(fields)


def _resolve_board_id(cmd: dict) -> int:
    """Board id from explicit board_id, else parsed off a legacy node string
    ("EPB_4" → 4). Matches fas_bridge._resolve_fas_board_id."""
    if "board_id" in cmd:
        return int(cmd["board_id"])
    node = str(cmd.get("node", ""))
    parts = node.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return max(0, int(parts[1]))
    return 0


# ---------------------------------------------------------------------------
# Command format validation (surfaced in the UI's command log)
# ---------------------------------------------------------------------------
_FAS_OPS = {"pwm_set", "load_sw_set", "imc_arm", "imc_disarm", "failsafe",
            "discover", "actuator_query", "buzzer", "sd_cmd"}
_CONSOLE_ACTIONS = {"start", "stop", "list_ports", "configure", "tx"}


def validate_command(topic: str, payload) -> tuple[bool, list[str]]:
    """Return (ok, issues) describing whether a received message matches the
    documented wire format. Best-effort — meant as a sanity check, not a spec."""
    issues: list[str] = []
    if not isinstance(payload, dict):
        return False, ["payload is not a JSON object"]

    source = payload.get("source")

    # nova/control: novaLock lockout, or a data_file echo.
    if topic == CONTROL_TOPIC:
        if isinstance(source, str) and source.lower() == "novalock":
            if payload.get("state") not in ("locked", "unlocked"):
                issues.append("novaLock 'state' must be 'locked' or 'unlocked'")
            return (not issues), issues
        # else fall through to command validation below

    cmd = payload.get("command")
    if not isinstance(cmd, dict):
        issues.append("missing 'command' object")
        return False, issues
    if topic == COMMAND_TOPIC and source != "novaOps":
        issues.append(f"source is {source!r}, expected 'novaOps' (will be ignored)")

    ctype = cmd.get("type")
    if ctype in ("relay", "gpio"):
        if not isinstance(cmd.get("id"), int):
            issues.append(f"{ctype}: 'id' must be an int")
        if cmd.get("state") not in (0, 1):
            issues.append(f"{ctype}: 'state' must be 0 or 1")
    elif ctype == "servo":
        if not isinstance(cmd.get("id"), int):
            issues.append("servo: 'id' must be an int")
        if "angle" not in cmd:
            issues.append("servo: missing 'angle' (int µs or 'on'/'off')")
    elif ctype == "fas":
        has_board = "board_id" in cmd or "node" in cmd
        if "port" in cmd:
            port = cmd.get("port")
            if port not in ("relay", "servo", "gpio"):
                issues.append(f"fas: port {port!r} not in relay/servo/gpio")
            if not has_board:
                issues.append("fas: need 'board_id' or 'node'")
            if port == "servo" and str(cmd.get("action", "")).lower() not in (
                    "enable", "disable") and "value" not in cmd:
                issues.append("fas servo: positional move needs 'value' (µs)")
        elif "op" in cmd:
            if cmd.get("op") not in _FAS_OPS:
                issues.append(f"fas: op {cmd.get('op')!r} unknown")
        else:
            issues.append("fas: needs a 'port' (high-level) or 'op' (direct) field")
    elif ctype == "console":
        if cmd.get("action") not in _CONSOLE_ACTIONS:
            issues.append(f"console: action {cmd.get('action')!r} not recognised")
    elif ctype == "data_file":
        if cmd.get("action") not in ("start_data_saving", "stop_data_saving"):
            issues.append(f"data_file: action {cmd.get('action')!r} invalid")
        if cmd.get("action") == "start_data_saving" and not cmd.get("filename"):
            issues.append("data_file start: missing 'filename'")
    else:
        issues.append(f"unknown command type {ctype!r}")

    return (not issues), issues


def _record_command(topic: str, payload) -> None:
    global _cmd_seq
    ok, issues = validate_command(topic, payload)
    with _lock:
        _cmd_seq += 1
        _recent.append({
            "seq": _cmd_seq,
            "t": int(time.time() * 1000),
            "topic": topic,
            "ok": ok,
            "issues": issues,
            "payload": payload,
        })


# ---------------------------------------------------------------------------
# Console (mocked two-way) — mirrors fas_bridge._cmd_console responses
# ---------------------------------------------------------------------------
def _publish_console(client: mqtt.Client, payload: dict) -> None:
    payload.setdefault("source", "novaGround")
    client.publish(CONSOLE_TOPIC, _dumps(payload), qos=0)


def _handle_console(client: mqtt.Client, cmd: dict) -> None:
    global _console_active
    action = str(cmd.get("action", "")).lower()

    if action in {"start", "stop"}:
        with _lock:
            _console_active = action == "start"
        print(f"[novaMock] console {action}")
        _publish_console(client, {"type": "console_status", "active": action == "start"})

    elif action == "list_ports":
        ports = [{"device": "/dev/ttySIM0", "name": "ttySIM0",
                  "description": "simulated FAS bridge", "hwid": "SIM"}]
        _publish_console(client, {"type": "console_ports", "ports": ports})

    elif action == "configure":
        port = str(cmd.get("port", "/dev/ttySIM0"))
        baud = int(cmd.get("baud", 460800))
        print(f"[novaMock] console configure -> {port} @ {baud}")
        _publish_console(client, {"type": "console_config", "ok": True,
                                  "port": port, "baud": baud})

    elif action == "tx":
        op = cmd.get("op")
        print(f"[novaMock] console tx op={op} board={cmd.get('board_id')} "
              f"ch={cmd.get('channel')}")
        _publish_console(client, {
            "type": "console_tx", "ok": True,
            "board_id": int(cmd.get("board_id", 0)),
            "channel": int(cmd.get("channel", 0)),
            "data_hex": "00" * 8,
        })

    else:
        print(f"[novaMock] console: unknown action {action!r}")


# ---------------------------------------------------------------------------
# FAS command handling — both the high-level port shape and the op shape,
# matching fas_bridge._cmd_fas_port / _cmd_fas_op.
# ---------------------------------------------------------------------------
def _handle_fas_port(cmd: dict) -> None:
    global _imc_armed, _imc_board_id
    board_id = _resolve_board_id(cmd)
    channel = int(cmd.get("channel", 0))
    port = str(cmd.get("port", ""))
    action = str(cmd.get("action", "")).lower()

    if port == "relay":
        enable = action == "on"
        _set_actuator(board_id, channel, load_sw_on=int(enable))
        print(f"[novaMock] fas load_sw EPB:{board_id} ch={channel} "
              f"{'ON' if enable else 'OFF'}")

    elif port == "servo":
        if action == "enable":
            print(f"[novaMock] fas servo enable (no-op) EPB:{board_id} ch={channel}")
            return
        pulse_us = 0 if action == "disable" else int(cmd.get("value", 0))
        _set_actuator(board_id, channel, pulse_us=pulse_us)
        print(f"[novaMock] fas pwm_set EPB:{board_id} ch={channel} pulse={pulse_us}us")

    elif port == "gpio":
        act = action.upper()
        if act == "ARM":
            with _lock:
                _imc_armed, _imc_board_id = True, board_id
            print(f"[novaMock] fas imc_arm EPB:{board_id}")
        elif act == "DISARM":
            with _lock:
                _imc_armed, _imc_board_id = False, board_id
            print(f"[novaMock] fas imc_disarm EPB:{board_id}")
        else:
            print(f"[novaMock] fas gpio: unknown action {action!r}")

    else:
        print(f"[novaMock] fas: unknown port {port!r}")


def _handle_fas_op(cmd: dict) -> None:
    global _imc_armed, _imc_board_id
    op = str(cmd.get("op", ""))
    board_id = int(cmd.get("board_id", 0))
    channel = int(cmd.get("channel", 0))

    if op == "pwm_set":
        _set_actuator(board_id, channel, pulse_us=int(cmd.get("pulse_us", 0)),
                      period_us=int(cmd.get("period_us", 20000)))
        print(f"[novaMock] fas op=pwm_set EPB:{board_id} ch={channel} "
              f"pulse={cmd.get('pulse_us')}us")
    elif op == "load_sw_set":
        _set_actuator(board_id, channel, load_sw_on=int(bool(cmd.get("enable"))))
        print(f"[novaMock] fas op=load_sw_set EPB:{board_id} ch={channel} "
              f"enable={cmd.get('enable')}")
    elif op == "imc_arm":
        with _lock:
            _imc_armed, _imc_board_id = True, board_id
        print(f"[novaMock] fas op=imc_arm board={board_id}")
    elif op == "imc_disarm":
        with _lock:
            _imc_armed, _imc_board_id = False, board_id
        print(f"[novaMock] fas op=imc_disarm board={board_id}")
    elif op == "failsafe":
        with _lock:
            _epb_actuators.pop(board_id, None)
        print(f"[novaMock] fas op=failsafe board={board_id}")
    elif op == "discover":
        print("[novaMock] fas op=discover")
    elif op == "actuator_query":
        print(f"[novaMock] fas op=actuator_query board={board_id} ch={channel}")
    elif op == "buzzer":
        notes = cmd.get("notes")
        if notes is not None:
            print(f"[novaMock] fas op=buzzer melody FMC:{board_id} notes={len(notes)}")
        else:
            print(f"[novaMock] fas op=buzzer FMC:{board_id} action={cmd.get('action')}")
    elif op == "sd_cmd":
        # Reflect SD logger control in the fake fill, like real firmware would.
        sub = str(cmd.get("action", "")).lower()
        with _lock:
            if sub == "clear":
                _sd["pct"] = 0.0
            elif sub == "set_rate":
                _sd["rate"] = max(0.0, float(cmd.get("divisor", cmd.get("arg", 1))))
        print(f"[novaMock] fas op=sd_cmd FMC:{board_id} action={sub}")
    else:
        print(f"[novaMock] fas: unknown op {op!r}")


def _handle_data_file(cmd: dict) -> None:
    action = cmd.get("action", "")
    if action == "start_data_saving":
        print(f"[novaMock] Data saving STARTED — file: {cmd.get('filename', 'unknown')}.csv")
    elif action == "stop_data_saving":
        print("[novaMock] Data saving STOPPED")
    else:
        print(f"[novaMock] Unknown data_file action: {action}")


# ---------------------------------------------------------------------------
# MQTT plumbing
# ---------------------------------------------------------------------------
def on_connect(client: mqtt.Client, _userdata, _flags, reason_code, _properties) -> None:
    ok = reason_code == 0 or (hasattr(reason_code, "is_failure") and not reason_code.is_failure)
    if ok:
        client.subscribe(COMMAND_TOPIC)
        client.subscribe(CONTROL_TOPIC)
        print(f"[novaMock] Connected to {BROKER}:{PORT}")
        print(f"[novaMock] Subscribed to {COMMAND_TOPIC} and {CONTROL_TOPIC}")
    else:
        print(f"[novaMock] Connect failed: {reason_code}")


def on_disconnect(_client, _userdata, _flags, reason_code, _properties) -> None:
    print(f"[novaMock] Disconnected: {reason_code}")


def on_message(client, _userdata, message) -> None:
    try:
        payload = json.loads(message.payload.decode("utf-8"))
    except Exception as exc:
        _record_command(message.topic, {"_decode_error": str(exc)})
        print(f"[novaMock] Error decoding message: {exc}")
        return

    # Capture every inbound message for the UI's validation log.
    _record_command(message.topic, payload)

    try:
        topic = message.topic
        cmd = payload.get("command", {}) if isinstance(payload, dict) else {}
        cmd_type = cmd.get("type", "")

        if topic == CONTROL_TOPIC:
            source = payload.get("source", "")
            if source.lower() == "novalock":
                print(f"[novaMock] Physical lockout: {payload.get('state', '?')}")
            elif cmd_type == "data_file":
                _handle_data_file(cmd)
            else:
                print(f"[novaMock] Control message: {payload}")
            return

        # nova/command — only act on the backend's own envelopes.
        if payload.get("source", "") != "novaOps":
            return

        # --- GCS device commands (novaGround side) ---
        if cmd_type == "relay":
            print(f"[novaMock] Relay command — id={cmd.get('id')} state={cmd.get('state')}")
        elif cmd_type == "servo":
            print(f"[novaMock] Servo command — id={cmd.get('id')} angle={cmd.get('angle')}")
        elif cmd_type == "gpio":
            print(f"[novaMock] GPIO command — id={cmd.get('id')} state={cmd.get('state')}")

        # --- FAS device commands (fas_bridge side) ---
        elif cmd_type == "fas":
            if "port" in cmd:
                _handle_fas_port(cmd)
            elif "op" in cmd:
                _handle_fas_op(cmd)
            else:
                print(f"[novaMock] fas: unrecognised shape {cmd}")

        elif cmd_type == "console":
            _handle_console(client, cmd)

        elif cmd_type == "data_file":
            _handle_data_file(cmd)

        else:
            print(f"[novaMock] Unhandled command type '{cmd_type}': {cmd}")

    except Exception as exc:
        print(f"[novaMock] Error processing message: {exc}")


def _maybe_publish_console_frame(client: mqtt.Client) -> None:
    """While console mode is on, mock an RX frame stream like fas_bridge does."""
    with _lock:
        active = _console_active
    if not active:
        return
    _publish_console(client, {
        "type": "fas_frame", "dir": "rx",
        "can_id": 0x01200000, "msg_type": 0x01, "board_kind": 2,
        "board_id": 0, "channel": 0,
        "data_hex": "00" * 8,
        "decoded": {"uptime_ms": int((time.time() - _boot_time) * 1000)},
    })


# ---------------------------------------------------------------------------
# Optional control UI (stdlib http.server; only started with --ui)
# ---------------------------------------------------------------------------
def _ui_state() -> dict:
    with _lock:
        sensors = {name: {k: c[k] for k in ("group", "wave", "min", "max",
                                            "period", "noise", "value")}
                   for name, c in SENSORS.items()}
        return {
            "sensors": sensors,
            "waves": WAVES,
            "flight_state": _flight_state,
            "flight_states": FLIGHT_STATES,
            "imc": {"armed": _imc_armed, "board_id": _imc_board_id},
            "console_active": _console_active,
            "sd": {"filling": _sd["filling"], "pct": round(_sd["pct"], 2),
                   "rate": _sd["rate"], "total_mb": _sd["total_mb"]},
            "launch": {
                "active": _launch["active"],
                "src": _launch["src"],
                "speed": _launch["speed"],
                "elapsed": round((time.time() - _launch["t0"]) * _launch["speed"], 1)
                           if _launch["active"] else 0.0,
                "duration": round(_launch["end_t"], 1),
                "default_csv": _sim_csv_default,
            },
        }


def _ui_apply_sensor(body: dict) -> None:
    name = body.get("name")
    with _lock:
        cfg = SENSORS.get(name)
        if not cfg:
            return
        if "wave" in body and body["wave"] in WAVES:
            cfg["wave"] = body["wave"]
        for key in ("min", "max", "period", "noise", "value"):
            if key in body and body[key] is not None:
                try:
                    cfg[key] = float(body[key])
                except (TypeError, ValueError):
                    pass


def _ui_apply_flight(body: dict) -> None:
    global _flight_state, _imc_armed
    with _lock:
        st = body.get("state")
        if st in FLIGHT_STATES:
            _flight_state = st
        if "imc_armed" in body:
            _imc_armed = bool(body["imc_armed"])


def _ui_apply_sd(body: dict) -> None:
    with _lock:
        if "filling" in body:
            _sd["filling"] = bool(body["filling"])
            _sd["last_tick"] = time.time()
        if "rate" in body and body["rate"] is not None:
            try:
                _sd["rate"] = max(0.0, float(body["rate"]))
            except (TypeError, ValueError):
                pass
        if "pct" in body and body["pct"] is not None:
            try:
                _sd["pct"] = max(0.0, min(100.0, float(body["pct"])))
            except (TypeError, ValueError):
                pass
        if body.get("reset"):
            _sd["pct"] = 0.0


def _ui_apply_launch(body: dict) -> str:
    """Start or abort a flight replay. Returns a short status string."""
    global _flight_state
    action = str(body.get("action", "")).lower()
    if action == "abort":
        with _lock:
            _launch["active"] = False
            _flight_state = "STANDBY"
        return "aborted"

    if action != "launch":
        return "unknown action"

    # Resolve the trajectory: explicit path, else the configured default CSV,
    # else a synthesised generic profile.
    path = str(body.get("path") or _sim_csv_default or "").strip()
    rows = load_sim_csv(path) if path else []
    if rows:
        src = f"csv:{os.path.basename(path)} ({len(rows)} pts)"
    else:
        rows = generic_profile()
        src = "generic profile"

    try:
        speed = max(0.1, float(body.get("speed", 1.0)))
    except (TypeError, ValueError):
        speed = 1.0

    idx = _index_trajectory(rows)
    with _lock:
        _launch.update({
            "active": True, "t0": time.time(), "speed": speed, "src": src,
            "samples": rows, "times": idx["times"],
            "apogee_t": idx["apogee_t"], "end_t": idx["end_t"],
        })
    print(f"[novaMock] LAUNCH — {src} x{speed} (apogee t={idx['apogee_t']:.1f}s, "
          f"duration {idx['end_t']:.1f}s)")
    return src


def _ui_commands(since: int) -> list:
    with _lock:
        return [c for c in _recent if c["seq"] > since]


def _make_ui_handler():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # keep the console quiet

        def _send(self, code, body, ctype="application/json"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                self._send(200, _UI_HTML, "text/html; charset=utf-8")
            elif parsed.path == "/api/state":
                self._send(200, json.dumps(_ui_state()))
            elif parsed.path == "/api/commands":
                qs = parse_qs(parsed.query)
                since = int(qs.get("since", ["0"])[0])
                self._send(200, json.dumps({"commands": _ui_commands(since)}))
            else:
                self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._send(400, json.dumps({"error": "bad json"}))
                return
            path = urlparse(self.path).path
            if path == "/api/sensor":
                _ui_apply_sensor(body)
            elif path == "/api/flight":
                _ui_apply_flight(body)
            elif path == "/api/sd":
                _ui_apply_sd(body)
            elif path == "/api/launch":
                _ui_apply_launch(body)
            else:
                self._send(404, json.dumps({"error": "not found"}))
                return
            self._send(200, json.dumps({"ok": True}))

    return Handler


def start_ui(port: int) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port), _make_ui_handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[novaMock] Control UI on http://localhost:{port}")


_UI_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>novaMock control</title>
<style>
  body{font:14px/1.4 system-ui,sans-serif;margin:0;background:#0e1116;color:#e6edf3}
  header{padding:10px 16px;background:#161b22;border-bottom:1px solid #30363d;
    display:flex;gap:16px;align-items:center;flex-wrap:wrap}
  h1{font-size:16px;margin:0}
  main{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding:14px}
  section{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px}
  h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:#9da7b3;margin:0 0 10px}
  table{width:100%;border-collapse:collapse}
  th,td{padding:4px 6px;text-align:left;border-bottom:1px solid #21262d;font-size:13px}
  th{color:#9da7b3;font-weight:600}
  input,select,button{background:#0d1117;color:#e6edf3;border:1px solid #30363d;
    border-radius:5px;padding:3px 6px;font:inherit}
  input[type=number]{width:64px}
  button{cursor:pointer}
  button:hover{border-color:#58a6ff}
  .pill{padding:1px 8px;border-radius:10px;font-size:12px}
  .on{background:#1f6feb33;color:#79c0ff}.off{background:#6e768166;color:#9da7b3}
  .grp{font-size:11px;color:#8b949e}
  #cmds{max-height:420px;overflow:auto;font-family:ui-monospace,Consolas,monospace;font-size:12px}
  .cmd{border-bottom:1px solid #21262d;padding:5px 4px}
  .ok{border-left:3px solid #3fb950}.bad{border-left:3px solid #f85149}
  .cmd .meta{color:#8b949e}
  .issues{color:#f85149;margin:2px 0 0}
  .bar{height:10px;background:#21262d;border-radius:5px;overflow:hidden;margin-top:6px}
  .bar>div{height:100%;background:#3fb950;width:0}
  .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0}
  .muted{color:#8b949e}
</style></head><body>
<header>
  <h1>novaMock control</h1>
  <span id="conn" class="muted">connecting…</span>
  <label><input type="checkbox" id="auto" checked> auto-refresh</label>
</header>
<main>
  <section style="grid-column:1/2">
    <h2>Sensors</h2>
    <table id="sensors"><thead><tr>
      <th>name</th><th>src</th><th>wave</th><th>min</th><th>max</th>
      <th>period</th><th>noise</th><th>value</th><th></th>
    </tr></thead><tbody></tbody></table>
    <p class="muted">value is used by the <b>flat</b> wave. period in seconds.</p>
  </section>
  <section style="grid-column:2/3">
    <h2>Flight state</h2>
    <div class="row">
      <select id="fstate"></select>
      <button onclick="setFlight()">Set state</button>
      <span class="muted">live:</span> <span id="livestate" class="pill on">?</span>
      <span id="imc" class="pill off">IMC ?</span>
    </div>
    <h2 style="margin-top:16px">Launch (trajectory replay)</h2>
    <div class="row">
      <button id="launchbtn" onclick="toggleLaunch()">🚀 Launch</button>
      speed <input type="number" id="lspeed" step="0.5" value="1" style="width:54px"> ×
    </div>
    <div class="row">
      CSV <input type="text" id="lpath" placeholder="(default / leave blank)" style="flex:1;min-width:180px">
    </div>
    <p class="muted">blank uses the default sim CSV, or a generic profile if none.
      <span id="lstatus"></span></p>
    <h2 style="margin-top:16px">SD card fill</h2>
    <div class="row">
      <button id="sdtoggle" onclick="toggleSd()">start fill</button>
      rate <input type="number" id="sdrate" step="0.1" value="1"> %/s
      <button onclick="sdReset()">reset</button>
    </div>
    <div>used <span id="sdpct">?</span>%</div>
    <div class="bar"><div id="sdbar"></div></div>
    <p class="muted">console mode: <span id="console">?</span></p>
  </section>
  <section style="grid-column:1/3">
    <h2>Received commands (live validation)</h2>
    <div id="cmds"></div>
  </section>
</main>
<script>
let lastSeq=0, state=null;
const $=id=>document.getElementById(id);
async function post(path,body){await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}
function num(v){const n=parseFloat(v);return isNaN(n)?null:n;}

function renderSensors(){
  const tb=$('sensors').querySelector('tbody');
  tb.innerHTML='';
  for(const[name,c]of Object.entries(state.sensors)){
    const tr=document.createElement('tr');tr.dataset.name=name;
    const opts=state.waves.map(w=>`<option ${w===c.wave?'selected':''}>${w}</option>`).join('');
    tr.innerHTML=`<td><b>${name}</b></td><td class="grp">${c.group}</td>
      <td><select class="w">${opts}</select></td>
      <td><input class="mn" type="number" step="0.01" value="${c.min}"></td>
      <td><input class="mx" type="number" step="0.01" value="${c.max}"></td>
      <td><input class="pd" type="number" step="0.5" value="${c.period}"></td>
      <td><input class="ns" type="number" step="0.001" value="${c.noise}"></td>
      <td><input class="vl" type="number" step="0.01" value="${c.value}"></td>
      <td><button>apply</button></td>`;
    tr.querySelector('button').onclick=()=>{
      post('/api/sensor',{name,wave:tr.querySelector('.w').value,
        min:num(tr.querySelector('.mn').value),max:num(tr.querySelector('.mx').value),
        period:num(tr.querySelector('.pd').value),noise:num(tr.querySelector('.ns').value),
        value:num(tr.querySelector('.vl').value)});
    };
    tb.appendChild(tr);
  }
}
function renderFlight(){
  const sel=$('fstate');
  if(sel.options.length!==state.flight_states.length){
    sel.innerHTML=state.flight_states.map(s=>`<option>${s}</option>`).join('');
    sel.value=state.flight_state;
  }
  // Don't clobber the dropdown while the user is choosing or a launch is live.
  if(document.activeElement!==sel && !state.launch.active){
    sel.value=state.flight_state;
  }
  $('livestate').textContent=state.flight_state;
  const imc=$('imc');
  imc.textContent='IMC '+(state.imc.armed?'ARMED':'safe')+' (b'+state.imc.board_id+')';
  imc.className='pill '+(state.imc.armed?'on':'off');
  $('console').textContent=state.console_active?'active':'off';
  $('sdpct').textContent=state.sd.pct;
  $('sdbar').style.width=state.sd.pct+'%';
  $('sdbar').style.background=state.sd.pct>=80?'#f85149':'#3fb950';
  $('sdtoggle').textContent=state.sd.filling?'stop fill':'start fill';
  // Launch
  const L=state.launch;
  $('launchbtn').textContent=L.active?'■ Abort':'🚀 Launch';
  if($('lpath').placeholder.indexOf('default')>=0 && L.default_csv){
    $('lpath').placeholder=L.default_csv;
  }
  $('lstatus').textContent=L.active
    ? `— ${L.src}, T+${L.elapsed}s / ${L.duration}s ×${L.speed}`
    : (L.src&&L.src!=='none'?`— last: ${L.src}`:'');
}
function setFlight(){post('/api/flight',{state:$('fstate').value});}
function toggleLaunch(){
  if(state.launch.active){post('/api/launch',{action:'abort'});}
  else{post('/api/launch',{action:'launch',speed:num($('lspeed').value)||1,path:$('lpath').value});}
}
function toggleSd(){post('/api/sd',{filling:!state.sd.filling,rate:num($('sdrate').value)});}
function sdReset(){post('/api/sd',{reset:true});}

async function refresh(){
  try{
    state=await(await fetch('/api/state')).json();
    $('conn').textContent='connected';$('conn').className='muted';
    if(!$('sensors').querySelector('tbody').children.length)renderSensors();
    renderFlight();
  }catch(e){$('conn').textContent='disconnected';}
}
async function pollCmds(){
  try{
    const r=await(await fetch('/api/commands?since='+lastSeq)).json();
    const box=$('cmds');
    for(const c of r.commands){
      lastSeq=Math.max(lastSeq,c.seq);
      const d=document.createElement('div');
      d.className='cmd '+(c.ok?'ok':'bad');
      const ts=new Date(c.t).toLocaleTimeString();
      d.innerHTML=`<div class="meta">#${c.seq} ${ts} ${c.topic} ${c.ok?'✓':'✗'}</div>`+
        `<div>${escapeHtml(JSON.stringify(c.payload))}</div>`+
        (c.issues.length?`<div class="issues">${c.issues.map(escapeHtml).join('<br>')}</div>`:'');
      box.insertBefore(d,box.firstChild);
    }
    while(box.children.length>200)box.removeChild(box.lastChild);
  }catch(e){}
}
function escapeHtml(s){return String(s).replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));}
setInterval(()=>{if($('auto').checked){refresh();pollCmds();}},1000);
refresh();pollCmds();
</script></body></html>"""


_DEFAULT_SIM_CSV = os.path.join(
    os.path.expanduser("~"),
    "OneDrive", "Documents", "UTAT", "Discovery", "OpenRocket", "Sim_Data.csv",
)


def main() -> None:
    global BROKER, PORT, HZ, FLIGHT_HZ, _sim_csv_default
    p = argparse.ArgumentParser(description="novaGround + FAS-bridge MQTT simulator")
    p.add_argument("--ui", action="store_true",
                   help="start the browser control panel (default: headless)")
    p.add_argument("--ui-port", type=int, default=8765, help="control panel port")
    p.add_argument("--broker", default=BROKER, help="MQTT broker host")
    p.add_argument("--port", type=int, default=PORT, help="MQTT broker port")
    p.add_argument("--hz", type=float, default=HZ, help="engine-sensor publish rate")
    p.add_argument("--flight-hz", type=float, default=FLIGHT_HZ, help="flight publish rate")
    p.add_argument("--sim-csv", default=_DEFAULT_SIM_CSV,
                   help="default trajectory CSV used by the UI 'Launch' button "
                        "(falls back to a synthetic profile if missing)")
    args = p.parse_args()

    BROKER, PORT, HZ, FLIGHT_HZ = args.broker, args.port, args.hz, args.flight_hz
    _sim_csv_default = args.sim_csv if os.path.isfile(args.sim_csv) else ""

    print(f"[novaMock] Starting (broker {BROKER}:{PORT}, engine {HZ} Hz, flight {FLIGHT_HZ} Hz)")
    if args.ui:
        start_ui(args.ui_port)

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=CLIENT_ID,
    )
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    # Connect in the background and keep retrying. connect_async never blocks the
    # main thread, so the UI works and Ctrl+C stays responsive even when no MQTT
    # broker is running yet (a blocking connect() would hang here otherwise).
    client.reconnect_delay_set(min_delay=1, max_delay=10)
    try:
        client.connect_async(BROKER, PORT, keepalive=60)
    except Exception as exc:  # bad host/port etc. — keep running so the UI lives
        print(f"[novaMock] connect_async error (will keep retrying): {exc}")
    client.loop_start()
    print(f"[novaMock] Connecting to MQTT in the background. If nothing connects, "
          f"start a broker (e.g. mosquitto) on {BROKER}:{PORT}.")

    sensor_interval = 1.0 / HZ
    flight_interval = 1.0 / FLIGHT_HZ
    next_flight = time.time()
    print(f"[novaMock] Publishing GCS + FAS engine sensors to {TELEMETRY_TOPIC} at {HZ} Hz")
    print(f"[novaMock] Publishing flight snapshot to {FLIGHT_TOPIC} at {FLIGHT_HZ} Hz")
    if not args.ui:
        print("[novaMock] (no UI; pass --ui for the control panel)")

    try:
        while True:
            now = time.time()
            client.publish(TELEMETRY_TOPIC, _dumps(build_gcs_packet()), qos=0)
            client.publish(TELEMETRY_TOPIC, _dumps(build_fas_engine_packet()), qos=0)
            if now >= next_flight:
                client.publish(FLIGHT_TOPIC, _dumps(build_flight_packet()), qos=0)
                _maybe_publish_console_frame(client)
                next_flight = now + flight_interval
            time.sleep(sensor_interval)
    except KeyboardInterrupt:
        print("\n[novaMock] Stopping")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
