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

Usage:
    python tools/novaMock.py

Environment variables:
    NOVA_MQTT_BROKER   MQTT broker host (default: localhost)
    NOVA_MQTT_PORT     MQTT broker port (default: 1883)
    NOVA_PUBLISH_HZ    Engine-sensor publish rate in Hz (default: 20)
    NOVA_FLIGHT_HZ     Flight telemetry publish rate in Hz (default: 4)
"""

import json
import math
import os
import random
import time

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

# ---------------------------------------------------------------------------
# GCS engine sensors — mirrors the GCS bindings in config/system.yaml
# (hat_id / channel_id). Published as a LIST under source="novaGround".
# ---------------------------------------------------------------------------
GCS_SENSORS = [
    {"hat_id": 0, "channel_id": 0, "label": "PGSO"},
    {"hat_id": 0, "channel_id": 1, "label": "PGS"},
    {"hat_id": 0, "channel_id": 4, "label": "PVO"},
    {"hat_id": 0, "channel_id": 6, "label": "MOT"},
    {"hat_id": 0, "channel_id": 7, "label": "MFT"},
    {"hat_id": 1, "channel_id": 1, "label": "CC-LC"},
]

# FAS engine sensors — mirrors the FAS bindings in config/system.yaml. The node
# string is "<KIND>_<board_id>" with board_id used directly (0-based), exactly
# how fas_bridge builds it from the wire (kind+board_id). Published as a DICT
# keyed "node:channel" under source="FAS".
#   PCC = EPB_0 ch1, POT = EPB_2 ch0, PFT = EPB_4 ch1
FAS_SENSORS = [
    {"node": "EPB_0", "channel": 1, "label": "PCC"},
    {"node": "EPB_2", "channel": 0, "label": "POT"},
    {"node": "EPB_4", "channel": 1, "label": "PFT"},
]

# Flight states (Flight_State_t / lookupTableFlightState in flight.h). The
# bridge only derives a coarse fsm state from IMC arm; we track a fuller state
# so SET_FLIGHT_STATE-style sims still look plausible.
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


# ---------------------------------------------------------------------------
# GCS engine sensor simulation (raw ADC volts, list form)
# ---------------------------------------------------------------------------
def _sim_voltage(label: str, t: float) -> float:
    if label in {"PGSO", "PGS"}:
        return 0.99 + 3 * (0.5 + 0.5 * math.sin(t * 0.3)) + random.uniform(-0.02, 0.02)
    if label in {"PVO"}:
        return 1.98 + 6 * (0.5 + 0.5 * math.sin(t * 0.3)) + random.uniform(-0.02, 0.02)
    if label in {"MOT", "MFT"}:
        return 3.59 + 8 * abs(math.sin(t * 0.1)) + random.uniform(-0.05, 0.05)
    if label in {"CC-LC"}:
        return 1.925 + 0.1 * (0.5 + 0.5 * math.sin(t * 0.2)) + random.uniform(-0.02, 0.02)
    return 2.5 + random.uniform(-0.1, 0.1)


def build_gcs_packet() -> dict:
    t = time.time()
    timestamp = int(t * 1000)
    return {
        "source": "novaGround",
        "sensors": [
            {
                "hat_id": s["hat_id"],
                "channel_id": s["channel_id"],
                "value": round(_sim_voltage(s["label"], t), 4),
                "timestamp": timestamp,
            }
            for s in GCS_SENSORS
        ],
    }


# ---------------------------------------------------------------------------
# FAS engine sensor simulation (scaled EPB ADC volts, dict form keyed
# "node:channel" — matches fas_bridge's _engine_values snapshot).
# ---------------------------------------------------------------------------
def _sim_epb_sensor(label: str, t: float) -> float:
    """Scaled volts for an EPB-backed pressure transducer (config raw range
    ~0.17-0.67 V over 0-900 psi)."""
    base = {"PFT": 0.2, "POT": 0.2, "PCC": 0.20}.get(label, 0.2)
    swing = {"PFT": 0.7, "POT": 0.7, "PCC": 0.7}.get(label, 0.10)
    return base + swing * (0.5 + 0.5 * math.sin(t * 0.25)) + random.uniform(-0.005, 0.005)


def build_fas_engine_packet() -> dict:
    t = time.time()
    timestamp = int(t * 1000)
    sensors = {
        f"{s['node']}:{s['channel']}": {
            "node": s["node"],
            "channel": s["channel"],
            "value": round(_sim_epb_sensor(s["label"], t), 6),
            "timestamp": timestamp,
        }
        for s in FAS_SENSORS
    }
    return {"source": "FAS", "sensors": sensors}


# ---------------------------------------------------------------------------
# Flight telemetry — nested board-state snapshot, mirroring fas_bridge's
# _publish_loop "data" block (fas_boards / fas_actuators / fas_sensors /
# fas_board_status / fas_fmc / fas_pmb / fas_imc / fas_fsm).
# ---------------------------------------------------------------------------
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


def _fmc_snapshot(t: float) -> dict:
    uptime = t - _boot_time
    ascent = _flight_state in {"POWERED_ASCENT", "COASTING", "APOGEE", "DESCENT"}
    pressure_pa = 101325.0 - (uptime * 12.0 if ascent else 0.0)
    altitude_m = max(0.0, (uptime * 30.0) if ascent else 0.0)
    return {
        "imu_accel": {
            "unit": "g",
            "axes": [round(random.uniform(-0.02, 0.02), 5),
                     round(random.uniform(-0.02, 0.02), 5),
                     round((4.0 if _flight_state == "POWERED_ASCENT" else 1.0)
                           + random.uniform(-0.01, 0.01), 5)],
        },
        "imu_gyro": {
            "unit": "dps",
            "axes": [round(random.uniform(-5, 5), 4),
                     round(random.uniform(-5, 5), 4),
                     round(random.uniform(-5, 5), 4)],
        },
        "accel_hg": {
            "unit": "g",
            "axes": [round(random.uniform(-0.02, 0.02), 5),
                     round(random.uniform(-0.02, 0.02), 5),
                     round((4.0 if _flight_state == "POWERED_ASCENT" else 1.0)
                           + random.uniform(-0.01, 0.01), 5)],
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
            "temp_c": round(24.0 + random.uniform(-0.3, 0.3), 2),
            "altitude_m": round(altitude_m + random.uniform(-1, 1), 2),
        },
        "gps_pos": {
            "lat": round(34.0561 + random.uniform(-1e-4, 1e-4), 7),
            "lon": round(-117.8443 + random.uniform(-1e-4, 1e-4), 7),
        },
        "gps_info": {
            "alt_m": int(altitude_m),
            "fix": 3,
            "sats": random.randint(7, 12),
            "hdop": round(random.uniform(0.8, 1.5), 1),
            "speed_mps": round(altitude_m / max(uptime, 1.0) if ascent else 0.0, 2),
        },
        "health": {"imu_ok": True, "accel_ok": True, "mag_ok": True,
                   "baro_ok": True, "gps_present": True},
        "temp": {"temp_h7": round(31.0 + 4.0 * math.sin(t * 0.1), 2),
                 "temp_pwr": round(36.0 + random.uniform(-0.4, 0.4), 2)},
        "sd": {"state": 3, "state_name": "logging", "err": 0, "pct_used": 12,
               "free_mb": 28000, "total_mb": 32000, "logging": True,
               "near_full": False, "full": False, "rate_reduced": False,
               "stalled": False, "rate_div": 1},
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

    actuators = {
        f"EPB:{bid}": [by_ch[c] for c in sorted(by_ch)]
        for bid, by_ch in _epb_actuators.items() if by_ch
    }

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
        "fas_fmc": {"FMC:0": _fmc_snapshot(t)},
        "fas_pmb": {"PMB:0": _pmb_snapshot(t)},
        "fas_imc": {
            "board_id": _imc_board_id,
            "armed": _imc_armed,
            "arm_line": _imc_armed,
            "disarm_line": not _imc_armed,
            "flags": 0,
        },
        "fas_fsm": {"state": "ARMED" if _imc_armed else _flight_state},
    }
    return {"source": "FAS", "data": data}


# ---------------------------------------------------------------------------
# Actuator-state bookkeeping (so the flight snapshot reflects commands)
# ---------------------------------------------------------------------------
def _set_actuator(board_id: int, channel: int, **fields) -> None:
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
# Console (mocked two-way) — mirrors fas_bridge._cmd_console responses
# ---------------------------------------------------------------------------
def _publish_console(client: mqtt.Client, payload: dict) -> None:
    payload.setdefault("source", "novaGround")
    client.publish(CONSOLE_TOPIC, json.dumps(payload), qos=0)


def _handle_console(client: mqtt.Client, cmd: dict) -> None:
    global _console_active
    action = str(cmd.get("action", "")).lower()

    if action in {"start", "stop"}:
        _console_active = action == "start"
        print(f"[novaMock] console {action}")
        _publish_console(client, {"type": "console_status", "active": _console_active})

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
        # Echo back a plausible console_tx ack without building a real frame.
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
            _imc_armed, _imc_board_id = True, board_id
            print(f"[novaMock] fas imc_arm EPB:{board_id}")
        elif act == "DISARM":
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
        _imc_armed, _imc_board_id = True, board_id
        print(f"[novaMock] fas op=imc_arm board={board_id}")
    elif op == "imc_disarm":
        _imc_armed, _imc_board_id = False, board_id
        print(f"[novaMock] fas op=imc_disarm board={board_id}")
    elif op == "failsafe":
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
        print(f"[novaMock] fas op=sd_cmd FMC:{board_id} action={cmd.get('action')}")
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
        topic = message.topic
        cmd = payload.get("command", {})
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
    if not _console_active:
        return
    _publish_console(client, {
        "type": "fas_frame", "dir": "rx",
        "can_id": 0x01200000, "msg_type": 0x01, "board_kind": 2,
        "board_id": 0, "channel": 0,
        "data_hex": "00" * 8,
        "decoded": {"uptime_ms": int((time.time() - _boot_time) * 1000)},
    })


def main() -> None:
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=CLIENT_ID,
    )
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    client.connect(BROKER, PORT, keepalive=60)
    client.loop_start()

    sensor_interval = 1.0 / HZ
    flight_interval = 1.0 / FLIGHT_HZ
    next_flight = time.time()
    print(f"[novaMock] Publishing GCS + FAS engine sensors to {TELEMETRY_TOPIC} at {HZ} Hz")
    print(f"[novaMock] Publishing flight snapshot to {FLIGHT_TOPIC} at {FLIGHT_HZ} Hz")

    try:
        while True:
            now = time.time()
            client.publish(TELEMETRY_TOPIC, json.dumps(build_gcs_packet()), qos=0)
            client.publish(TELEMETRY_TOPIC, json.dumps(build_fas_engine_packet()), qos=0)
            if now >= next_flight:
                client.publish(FLIGHT_TOPIC, json.dumps(build_flight_packet()), qos=0)
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
