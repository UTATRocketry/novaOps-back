#!/usr/bin/env python3
"""
fas_bridge.py  —  Direct FAS RS-422 to MQTT bridge.

Runs on any machine with a serial port connected to the FAS FMC bridge.
Publishes nova/telemetry/engine and nova/telemetry/flight in the same format
as novaGround so the novaOps backend sees no difference. Translates inbound
nova/command FAS commands to wire frames. All non-FAS command types are
silently dropped.

Telemetry published
  nova/telemetry/engine — sensors: FAS EPB ADC samples at hat_id = (100 + board_id)
                          gpios:   always empty (bridge has no GPIO manager)
  nova/telemetry/flight — full board state snapshot, mirroring the gs server:
                          fas_boards:       online/uptime/fw/caps per board
                          fas_actuators:    per-channel actuator state (EPB)
                          fas_sensors:      SENSOR_STATUS masks per board
                          fas_board_status: EPB bus voltage / current rails
                          fas_fmc:          FMC IMU/baro/GPS/temp/SD/radio
                          fas_pmb:          PMB power/vmon/temp/charger
                          fas_imc:          IMC arm/disarm state
  All message types in gs/protocol.py are decoded via decode_payload().

Inbound commands handled
  {"type":"fas",     ...}   — both op-shape and board_type/port/action shape
  {"type":"console", ...}   — "start" publishes raw frame JSON to nova/console
  everything else           — silently dropped

Dependencies: pip install paho-mqtt pyserial

Usage:
    python fas_bridge.py --port /dev/ttyUSB0 [--baud 460800]
                         [--broker localhost:1883] [--node-id fas_bridge]
                         [--publish-ms 50] [--verbosity 0|1|2]
"""
from __future__ import annotations

import argparse
import json
import struct
import threading
import time
from collections import deque
from enum import IntEnum

import paho.mqtt.client as mqtt
import serial

# ── Topics ──────────────────────────────────────────────────────────────────
COMMAND_TOPIC   = "nova/command"
TELEMETRY_TOPIC = "nova/telemetry/engine"
FLIGHT_TOPIC    = "nova/telemetry/flight"
CONSOLE_TOPIC   = "nova/console"
EXPECTED_SOURCE = "novaOps"

# ── FAS wire constants ───────────────────────────────────────────────────────
RS422_MAGIC       = 0xAA
RS422_MAX_PAYLOAD = 12          # 4-byte CAN ID + up to 8 data bytes

# Board kinds
RT_BOARD_GS  = 0
RT_BOARD_FMC = 1
RT_BOARD_EPB = 2
RT_BOARD_IMC = 3
RT_BOARD_RAB = 4
RT_BOARD_PMB = 5
BOARD_KIND_NAMES = {0: "GS", 1: "FMC", 2: "EPB", 3: "IMC", 4: "RAB", 5: "PMB"}

# Message types (rt_msg_t) — full set, mirrors gs/protocol.py MsgType
RT_MSG_HEARTBEAT          = 0x01
RT_MSG_DISCOVERY_REQ      = 0x02
RT_MSG_DISCOVERY_ANNOUNCE = 0x03
RT_MSG_TIME_SYNC          = 0x04
RT_MSG_PWM_SET            = 0x10
RT_MSG_LOAD_SW_SET        = 0x11
RT_MSG_ACTUATOR_QUERY     = 0x12
RT_MSG_ACTUATOR_CONFIG    = 0x13
RT_MSG_ACTUATOR_STATE     = 0x14
RT_MSG_ACTUATOR_FAILSAFE  = 0x1F
RT_MSG_ADC_BURST          = 0x20
RT_MSG_SENSOR_STATUS      = 0x21
RT_MSG_BOARD_STATUS       = 0x22
RT_MSG_ADC_SAMPLE         = 0x23
RT_MSG_FMC_IMU_ACCEL      = 0x24
RT_MSG_FMC_IMU_GYRO       = 0x25
RT_MSG_FMC_ACCEL_HG       = 0x26
RT_MSG_FMC_MAG            = 0x27
RT_MSG_FMC_BARO           = 0x28
RT_MSG_FMC_GPS_POS        = 0x29
RT_MSG_FMC_GPS_INFO       = 0x2A
RT_MSG_FMC_HEALTH         = 0x2B
RT_MSG_PMB_PWR            = 0x2C
RT_MSG_PMB_VMON           = 0x2D
RT_MSG_PMB_TEMP           = 0x2E
RT_MSG_FMC_TEMP           = 0x2F
RT_MSG_IGN_ARM            = 0x30
RT_MSG_IGN_FIRE           = 0x31
RT_MSG_IGN_DISARM         = 0x32
RT_MSG_IMC_STATUS         = 0x33
RT_MSG_FMC_SD_STATUS      = 0x34
RT_MSG_FMC_RADIO_STATUS   = 0x35
RT_MSG_PMB_CHARGER        = 0x39

# Names for FMC vector sensors, keyed by msg type → snapshot field name
FMC_VEC3_FIELDS = {
    RT_MSG_FMC_IMU_ACCEL: "imu_accel",
    RT_MSG_FMC_IMU_GYRO:  "imu_gyro",
    RT_MSG_FMC_ACCEL_HG:  "accel_hg",
    RT_MSG_FMC_MAG:       "mag",
}

# ADC scaling: firmware shifts 24-bit code right by 8 → int16
ADC_INT16_TO_V  = 256.0 * 1.2 / (1 << 23)
ADC_INT16_TO_MA = ADC_INT16_TO_V * 1000.0 / 100.0

# Heartbeat timeout matching loops.cpp kBoardTimeoutS
BOARD_TIMEOUT_S   = 3.0
DISCOVERY_INTERVAL_S = 2.0


# ── CAN ID pack / unpack ─────────────────────────────────────────────────────

def can_id_pack(msg: int, kind: int, board_id: int, channel: int,
                flags: int, seq: int) -> int:
    return (((msg & 0x3F) << 23) | ((kind & 0x07) << 20)
            | ((board_id & 0x07) << 17) | ((channel & 0x3F) << 11)
            | ((flags & 0x07) << 8) | (seq & 0xFF))


def can_id_unpack(can_id: int) -> dict:
    return {
        "msg":      (can_id >> 23) & 0x3F,
        "kind":     (can_id >> 20) & 0x07,
        "board_id": (can_id >> 17) & 0x07,
        "channel":  (can_id >> 11) & 0x3F,
        "flags":    (can_id >> 8)  & 0x07,
        "seq":      can_id         & 0xFF,
    }


# ── CRC-16/CCITT-FALSE (matches rt_crc16 in rt_proto.c) ─────────────────────

def crc16(data: bytes | bytearray) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
        crc &= 0xFFFF
    return crc


# ── RS-422 frame codec ───────────────────────────────────────────────────────

def encode_frame(can_id: int, data: bytes) -> bytes:
    """Build a complete RS-422 frame for the given CAN ID and payload bytes."""
    payload   = struct.pack("<I", can_id) + data
    length    = len(payload)
    len_bytes = struct.pack("<H", length)
    checksum  = crc16(len_bytes + payload)
    return bytes([RS422_MAGIC]) + len_bytes + payload + struct.pack("<H", checksum)


class _ParserState(IntEnum):
    WAIT_MAGIC   = 0
    WAIT_LEN_LO  = 1
    WAIT_LEN_HI  = 2
    READ_PAYLOAD = 3
    WAIT_CRC_LO  = 4
    WAIT_CRC_HI  = 5


class FrameParser:
    """Streaming RS-422 frame parser, mirrors FasSerial::step() in C++."""

    def __init__(self, callback) -> None:
        self._cb      = callback   # callback(can_id: int, data: bytes)
        self._state   = _ParserState.WAIT_MAGIC
        self._len     = 0
        self._buf     = bytearray()
        self._crc_lo  = 0
        self.frames_decoded = 0
        self.frames_dropped = 0

    def feed(self, data: bytes) -> None:
        for b in data:
            self._step(b)

    def _step(self, b: int) -> None:
        s = self._state

        if s == _ParserState.WAIT_MAGIC:
            if b == RS422_MAGIC:
                self._state = _ParserState.WAIT_LEN_LO

        elif s == _ParserState.WAIT_LEN_LO:
            self._len = b
            self._state = _ParserState.WAIT_LEN_HI

        elif s == _ParserState.WAIT_LEN_HI:
            self._len |= b << 8
            if 4 <= self._len <= RS422_MAX_PAYLOAD:
                self._buf.clear()
                self._state = _ParserState.READ_PAYLOAD
            else:
                self.frames_dropped += 1
                self._state = _ParserState.WAIT_MAGIC

        elif s == _ParserState.READ_PAYLOAD:
            self._buf.append(b)
            if len(self._buf) == self._len:
                self._state = _ParserState.WAIT_CRC_LO

        elif s == _ParserState.WAIT_CRC_LO:
            self._crc_lo = b
            self._state  = _ParserState.WAIT_CRC_HI

        elif s == _ParserState.WAIT_CRC_HI:
            received_crc = self._crc_lo | (b << 8)
            len_bytes    = struct.pack("<H", self._len)
            expected_crc = crc16(len_bytes + self._buf)

            if received_crc == expected_crc:
                can_id = struct.unpack_from("<I", self._buf, 0)[0]
                data   = bytes(self._buf[4:])
                self.frames_decoded += 1
                self._cb(can_id, data)
            else:
                self.frames_dropped += 1

            self._state = _ParserState.WAIT_MAGIC


# ── Inbound payload decoder (mirrors gs/protocol.py unpack_payload) ───────────

def decode_payload(msg: int, data: bytes) -> dict:
    """Decode a frame payload into a dict of fields, keyed by message type.

    Mirrors unpack_payload() in gs/protocol.py byte-for-byte. Unknown or
    too-short payloads fall back to {"raw_hex": ...} so nothing is lost.
    """
    def fits(fmt: str) -> bool:
        return len(data) >= struct.calcsize(fmt)

    if msg == RT_MSG_HEARTBEAT and fits("<IHBB"):
        uptime, fw, bid, flags = struct.unpack_from("<IHBB", data, 0)
        return {"uptime_ms": uptime, "fw_version": fw, "board_id": bid, "flags": flags}
    if msg == RT_MSG_DISCOVERY_ANNOUNCE and fits("<BBBBHBB"):
        kind, bid, nch, nse, fw, caps, _ = struct.unpack_from("<BBBBHBB", data, 0)
        return {"board_kind": kind, "board_id": bid, "num_channels": nch,
                "num_sensors": nse, "fw_version": fw, "caps_mask": caps}
    if msg == RT_MSG_ACTUATOR_CONFIG and fits("<BBBB4s"):
        ch, caps, safe, _, name = struct.unpack_from("<BBBB4s", data, 0)
        return {"channel_idx": ch, "caps": caps, "safe_state": safe,
                "short_name": name.decode("ascii", errors="replace").rstrip("\x00")}
    if msg == RT_MSG_ACTUATOR_STATE and fits("<BBHHBB"):
        ch, on, pulse, period, faults, _ = struct.unpack_from("<BBHHBB", data, 0)
        return {"channel_idx": ch, "load_sw_on": on, "pulse_us": pulse,
                "period_us": period, "fault_bits": faults}
    if msg == RT_MSG_ADC_BURST and fits("<4h"):
        c0, c1, c2, c3 = struct.unpack_from("<4h", data, 0)
        return {"ch": [c0, c1, c2, c3]}
    if msg == RT_MSG_ADC_SAMPLE and fits("<Ihh"):
        t_us, c0, c1 = struct.unpack_from("<Ihh", data, 0)
        return {"t_us": t_us, "ch": [c0, c1]}
    if msg in FMC_VEC3_FIELDS and fits("<3hH"):
        x, y, z, t_ms = struct.unpack_from("<3hH", data, 0)
        return {"axes": [x, y, z], "t_ms": t_ms}
    if msg == RT_MSG_FMC_BARO and fits("<ih"):
        pressure_pa, temp_cc = struct.unpack_from("<ih", data, 0)
        return {"pressure_pa": pressure_pa, "temp_cc": temp_cc}
    if msg == RT_MSG_FMC_GPS_POS and fits("<ii"):
        lat_1e7, lon_1e7 = struct.unpack_from("<ii", data, 0)
        return {"lat_1e7": lat_1e7, "lon_1e7": lon_1e7}
    if msg == RT_MSG_FMC_GPS_INFO and fits("<hBBHH"):
        alt_m, fix, sats, hdop_x10, speed_cms = struct.unpack_from("<hBBHH", data, 0)
        return {"alt_m": alt_m, "fix": fix, "sats": sats,
                "hdop_x10": hdop_x10, "speed_cms": speed_cms}
    if msg == RT_MSG_FMC_HEALTH and fits("<BBBBHBB"):
        imu, accel, mag, present, baro_c1, gfix, gsats = struct.unpack_from("<BBBBHBB", data, 0)
        return {"imu_id": imu, "accel_id": accel, "mag_id": mag,
                "present_mask": present, "baro_c1": baro_c1,
                "gps_fix": gfix, "gps_sats": gsats}
    if msg == RT_MSG_PMB_PWR and fits("<HHHH"):
        v8, i8, v24, i24 = struct.unpack_from("<HHHH", data, 0)
        return {"v_8v4_mv": v8, "i_8v4_ma": i8, "v_24v0_mv": v24, "i_24v0_ma": i24}
    if msg == RT_MSG_PMB_VMON and fits("<HHHBB"):
        vmain, vbatt, vgse, flags, _ = struct.unpack_from("<HHHBB", data, 0)
        return {"v_main_mv": vmain, "v_batt_mv": vbatt, "v_gse_mv": vgse, "flags": flags}
    if msg == RT_MSG_PMB_TEMP and fits("<hhhH"):
        ta, tb, tc, _ = struct.unpack_from("<hhhH", data, 0)
        return {"temp_amb_cc": ta, "temp_buck_cc": tb, "temp_boost_cc": tc}
    if msg == RT_MSG_PMB_CHARGER and fits("<hHBBBB"):
        i_chg, v_bat, flags, state, status, cells = struct.unpack_from("<hHBBBB", data, 0)
        return {"i_chg_ma": i_chg, "v_bat_mv": v_bat, "flags": flags,
                "state": state, "status": status, "cells": cells}
    if msg == RT_MSG_BOARD_STATUS and fits("<HHHH"):
        v8, v24, i8, i24 = struct.unpack_from("<HHHH", data, 0)
        return {"vmon_8v4_mv": v8, "vmon_24v_mv": v24,
                "isense_8v4_ma": i8, "isense_24v_ma": i24}
    if msg == RT_MSG_FMC_TEMP and fits("<hhI"):
        t_h7, t_pwr, _ = struct.unpack_from("<hhI", data, 0)
        return {"temp_h7_cc": t_h7, "temp_pwr_cc": t_pwr}
    if msg == RT_MSG_FMC_SD_STATUS and fits("<BBHI"):
        state, err, free_mb, written_kb = struct.unpack_from("<BBHI", data, 0)
        return {"state": state, "err": err, "free_mb": free_mb, "written_kb": written_kb}
    if msg == RT_MSG_FMC_RADIO_STATUS and fits("<BBHI"):
        flags, every_n, tx_frames, tx_bytes = struct.unpack_from("<BBHI", data, 0)
        return {"flags": flags, "every_n": every_n,
                "tx_frames": tx_frames, "tx_bytes": tx_bytes}
    if msg == RT_MSG_TIME_SYNC and fits("<II"):
        t_us, _ = struct.unpack_from("<II", data, 0)
        return {"t_us": t_us}
    if msg == RT_MSG_SENSOR_STATUS and fits("<BBBBI"):
        conn, sat, err, _, _ = struct.unpack_from("<BBBBI", data, 0)
        return {"connected_mask": conn, "saturated_mask": sat, "error_mask": err}
    if msg == RT_MSG_IMC_STATUS and fits("<BBBBI"):
        armed, arm_line, disarm_line, flags, _ = struct.unpack_from("<BBBBI", data, 0)
        return {"armed": armed, "arm_line": arm_line,
                "disarm_line": disarm_line, "flags": flags}
    return {"raw_hex": data.hex()}


# ── Outbound payload builders ────────────────────────────────────────────────

def _encode_pwm_set(duty_q15: int, period_us: int) -> bytes:
    # rt_pwm_set_t: uint16 duty_q15, uint16 period_us, uint32 reserved
    return struct.pack("<HHI", duty_q15 & 0xFFFF, period_us & 0xFFFF, 0)


def _encode_load_sw_set(enable: bool, hold_ms: int) -> bytes:
    # rt_load_sw_set_t: uint8 enable, uint8 reserved, uint16 hold_ms, uint32 reserved
    return struct.pack("<BBHI", int(enable), 0, hold_ms & 0xFFFF, 0)


def _encode_imc_cmd(pulse_ms: int) -> bytes:
    # rt_imc_cmd_t: uint16 pulse_ms, uint8 reserved x2, uint32 reserved
    return struct.pack("<HBBI", pulse_ms & 0xFFFF, 0, 0, 0)


def _pad8() -> bytes:
    return b"\x00" * 8


def pulse_to_q15(pulse_us: int, period_us: int = 20000) -> int:
    p = period_us if period_us > 0 else 20000
    return min(32767, (pulse_us * 32767) // p)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_broker(broker: str) -> tuple[str, int]:
    broker = broker.removeprefix("mqtt://")
    if ":" in broker:
        h, p = broker.rsplit(":", 1)
        return h, int(p)
    return broker, 1883


def _resolve_fas_board_id(cmd: dict) -> int:
    if "board_id" in cmd:
        return int(cmd["board_id"])
    node = str(cmd.get("node", ""))
    if node:
        parts = node.rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            return max(0, int(parts[1]) - 1)
    return 0


# ─────────────────────────────────────────────────────────────────────────────
class FasBridge:
    def __init__(self, port: str, baud: int, broker: str,
                 node_id: str, publish_ms: int, verbosity: int) -> None:
        self._port       = port
        self._baud       = baud
        self._node_id    = node_id
        self._publish_ms = publish_ms
        self._verbosity  = verbosity
        self._lock       = threading.Lock()
        self._seq        = 0

        # Telemetry state — mirrors gs StateModel: boards, actuators, sensors, imc
        # plus FMC/PMB onboard telemetry, all keyed by board key ("EPB:0", etc.)
        self._adc_samples: deque[dict] = deque(maxlen=1024)
        self._fas_boards: dict[str, dict]  = {}    # key → {online, uptime_ms, last_seen, ...}
        self._fas_actuators: dict[str, dict[int, dict]] = {}  # key → {channel_idx → state}
        self._fas_sensors: dict[str, dict] = {}    # key → SENSOR_STATUS fields
        self._fas_board_status: dict[str, dict] = {}  # key → BOARD_STATUS V+I
        self._fas_fmc: dict[str, dict] = {}        # key → merged FMC sensor fields
        self._fas_pmb: dict[str, dict] = {}        # key → merged PMB telemetry fields
        self._fas_imc  = {"board_id": 0, "armed": False,
                           "arm_line": False, "disarm_line": False}
        self._console_active = False

        # Serial
        self._serial  = serial.Serial(port, baud, timeout=1)
        self._parser  = FrameParser(self._on_frame)

        # MQTT
        self._client = mqtt.Client(client_id=node_id, clean_session=True)
        self._client.on_connect    = self._on_connect
        self._client.on_message    = self._on_message
        self._client.on_disconnect = self._on_disconnect
        host, port_num = _parse_broker(broker)
        self._client.connect(host, port_num, keepalive=60)

    # ── MQTT callbacks ───────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc: int) -> None:
        if rc == 0:
            self._log(1, f"[bridge] MQTT connected as '{self._node_id}'")
            client.subscribe(COMMAND_TOPIC, qos=1)
        else:
            self._log(0, f"[bridge] MQTT connect failed rc={rc}")

    def _on_disconnect(self, client, userdata, rc: int) -> None:
        self._log(1, f"[bridge] MQTT disconnected rc={rc}")

    def _on_message(self, client, userdata, msg) -> None:
        try:
            env = json.loads(msg.payload)
        except Exception:
            return
        if not isinstance(env, dict) or env.get("source") != EXPECTED_SOURCE:
            return
        cmd = env.get("command")
        if isinstance(cmd, dict):
            self._dispatch_command(cmd)

    # ── Command dispatch ─────────────────────────────────────────────────────

    def _dispatch_command(self, cmd: dict) -> None:
        t = cmd.get("type", "")
        if t == "fas":
            if "port" in cmd:
                self._cmd_fas_port(cmd)
            elif "op" in cmd:
                self._cmd_fas_op(cmd)
            else:
                self._log(2, f"[bridge] fas: unrecognised shape {cmd}")
        elif t == "console":
            action = str(cmd.get("action", "")).lower()
            with self._lock:
                self._console_active = action == "start"
            self._log(1, f"[bridge] console {'started' if self._console_active else 'stopped'}")
        # All other types are silently dropped per spec.

    def _cmd_fas_port(self, cmd: dict) -> None:
        board_id = _resolve_fas_board_id(cmd)
        channel  = int(cmd.get("channel", 0))
        port     = str(cmd.get("port", ""))
        action   = str(cmd.get("action", "")).lower()

        if port == "relay":
            enable  = action == "on"
            hold_ms = int(cmd.get("hold_ms", 0))
            self._send_load_sw_set(board_id, channel, enable, hold_ms)
            self._log(1, f"[bridge] fas load_sw EPB:{board_id} ch={channel} "
                         f"{'ON' if enable else 'OFF'}")

        elif port == "servo":
            if action == "enable":
                self._log(2, f"[bridge] fas servo enable (no-op) EPB:{board_id} ch={channel}")
                return
            pulse_us  = 0 if action == "disable" else int(cmd.get("value", 0))
            period_us = int(cmd.get("period_us", 20000))
            self._send_pwm_set(board_id, channel, pulse_us, period_us)
            self._log(1, f"[bridge] fas pwm_set EPB:{board_id} ch={channel} "
                         f"pulse={pulse_us}µs")

        elif port == "gpio":
            act = action.upper()
            if act == "ARM":
                self._send_imc_arm(board_id, 0)
                self._log(1, f"[bridge] fas imc_arm EPB:{board_id}")
            elif act == "DISARM":
                self._send_imc_disarm(board_id, 0)
                self._log(1, f"[bridge] fas imc_disarm EPB:{board_id}")
            else:
                self._log(2, f"[bridge] fas gpio: unknown action {action!r}")
        else:
            self._log(2, f"[bridge] fas: unknown port {port!r}")

    def _cmd_fas_op(self, cmd: dict) -> None:
        op       = str(cmd.get("op", ""))
        board_id = int(cmd.get("board_id", 0))
        channel  = int(cmd.get("channel", 0))

        if op == "pwm_set":
            pulse_us  = int(cmd.get("pulse_us", 0))
            period_us = int(cmd.get("period_us", 20000))
            self._send_pwm_set(board_id, channel, pulse_us, period_us)
            self._log(1, f"[bridge] fas pwm_set EPB:{board_id} ch={channel} "
                         f"pulse={pulse_us}µs")

        elif op == "load_sw_set":
            enable  = bool(cmd.get("enable", False))
            hold_ms = int(cmd.get("hold_ms", 0))
            self._send_load_sw_set(board_id, channel, enable, hold_ms)
            self._log(1, f"[bridge] fas load_sw EPB:{board_id} ch={channel} "
                         f"enable={enable}")

        elif op == "failsafe":
            cid   = can_id_pack(RT_MSG_ACTUATOR_FAILSAFE, RT_BOARD_GS, board_id, 0, 0, self._next_seq())
            self._send_frame(cid, _pad8())
            self._log(1, f"[bridge] fas failsafe EPB:{board_id}")

        elif op == "imc_arm":
            pulse_ms = int(cmd.get("pulse_ms", 0))
            self._send_imc_arm(board_id, pulse_ms)
            self._log(1, f"[bridge] fas imc_arm EPB:{board_id} pulse={pulse_ms}ms")

        elif op == "imc_disarm":
            pulse_ms = int(cmd.get("pulse_ms", 0))
            self._send_imc_disarm(board_id, pulse_ms)
            self._log(1, f"[bridge] fas imc_disarm EPB:{board_id} pulse={pulse_ms}ms")

        elif op == "discover":
            self._send_discovery_req()
            self._log(2, "[bridge] fas discover sent")

        elif op == "actuator_query":
            cid = can_id_pack(RT_MSG_ACTUATOR_QUERY, RT_BOARD_GS, board_id, channel, 0, self._next_seq())
            self._send_frame(cid, _pad8())
            self._log(1, f"[bridge] fas actuator_query EPB:{board_id} ch={channel}")

        else:
            self._log(2, f"[bridge] fas: unknown op {op!r}")

    # ── FAS frame receive ────────────────────────────────────────────────────

    def _on_frame(self, can_id: int, data: bytes) -> None:
        cid = can_id_unpack(can_id)
        msg = cid["msg"]

        # Console raw-frame output
        with self._lock:
            console = self._console_active
        if console:
            hex_str = " ".join(f"{b:02x}" for b in data)
            frame = {
                "source":     "novaGround",
                "type":       "fas_frame",
                "can_id":     can_id,
                "msg_type":   msg,
                "board_kind": cid["kind"],
                "board_id":   cid["board_id"],
                "channel":    cid["channel"],
                "data_hex":   hex_str,
            }
            self._client.publish(CONSOLE_TOPIC, json.dumps(frame), qos=0)

        board_id  = cid["board_id"]
        kind_name = BOARD_KIND_NAMES.get(cid["kind"], "UNK")
        key       = f"{kind_name}:{board_id}"
        now       = time.monotonic()
        d         = decode_payload(msg, data)

        if msg == RT_MSG_HEARTBEAT:
            with self._lock:
                prev = self._fas_boards.get(key, {})
                self._fas_boards[key] = {
                    **prev,
                    "kind":       kind_name,
                    "board_id":   board_id,
                    "online":     True,
                    "uptime_ms":  d.get("uptime_ms", 0),
                    "fw_version": d.get("fw_version", prev.get("fw_version", 0)),
                    "last_seen":  now,
                    "num_channels": prev.get("num_channels", 0),
                    "num_sensors":  prev.get("num_sensors", 0),
                }
            self._log(2, f"[bridge] heartbeat {key} uptime={d.get('uptime_ms')}ms")

        elif msg == RT_MSG_DISCOVERY_ANNOUNCE:
            with self._lock:
                prev = self._fas_boards.get(key, {})
                self._fas_boards[key] = {
                    **prev,
                    "kind":         kind_name,
                    "board_id":     board_id,
                    "online":       True,
                    "uptime_ms":    prev.get("uptime_ms", 0),
                    "last_seen":    now,
                    "num_channels": d.get("num_channels", 0),
                    "num_sensors":  d.get("num_sensors", 0),
                    "caps_mask":    d.get("caps_mask", 0),
                    "fw_version":   d.get("fw_version", prev.get("fw_version", 0)),
                }
            self._log(1, f"[bridge] announce {key} fw={d.get('fw_version', 0):#06x} "
                         f"ch={d.get('num_channels')} sens={d.get('num_sensors')}")

        elif msg in (RT_MSG_ADC_SAMPLE, RT_MSG_ADC_BURST):
            # Collapse both stamped samples and legacy bursts into the ADC queue.
            ch = d.get("ch", [])
            t_us = d.get("t_us", 0)
            now_ms = int(now * 1000)
            with self._lock:
                self._adc_samples.append({
                    "board_id": board_id,
                    "t_us":     t_us,
                    "v0":       (ch[0] if len(ch) > 0 else 0) * ADC_INT16_TO_V,
                    "v1":       (ch[1] if len(ch) > 1 else 0) * ADC_INT16_TO_V,
                    "ma0":      (ch[0] if len(ch) > 0 else 0) * ADC_INT16_TO_MA,
                    "ma1":      (ch[1] if len(ch) > 1 else 0) * ADC_INT16_TO_MA,
                    "ts_ms":    now_ms,
                })

        elif msg == RT_MSG_ACTUATOR_STATE:
            ch = d.get("channel_idx", 0)
            with self._lock:
                self._fas_actuators.setdefault(key, {})[ch] = d
            self._log(2, f"[bridge] actuator {key} ch={ch} pulse={d.get('pulse_us')}us")

        elif msg == RT_MSG_SENSOR_STATUS:
            with self._lock:
                self._fas_sensors[key] = d
            self._log(2, f"[bridge] sensor status {key} conn={d.get('connected_mask')}")

        elif msg == RT_MSG_BOARD_STATUS:
            with self._lock:
                self._fas_board_status[key] = d
            self._log(2, f"[bridge] board status {key} v8={d.get('vmon_8v4_mv')}mV")

        elif msg in FMC_VEC3_FIELDS:
            field = FMC_VEC3_FIELDS[msg]
            with self._lock:
                self._fas_fmc.setdefault(key, {})[field] = d

        elif msg in (RT_MSG_FMC_BARO, RT_MSG_FMC_GPS_POS, RT_MSG_FMC_GPS_INFO,
                     RT_MSG_FMC_HEALTH, RT_MSG_FMC_TEMP,
                     RT_MSG_FMC_SD_STATUS, RT_MSG_FMC_RADIO_STATUS):
            field = {
                RT_MSG_FMC_BARO:         "baro",
                RT_MSG_FMC_GPS_POS:      "gps_pos",
                RT_MSG_FMC_GPS_INFO:     "gps_info",
                RT_MSG_FMC_HEALTH:       "health",
                RT_MSG_FMC_TEMP:         "temp",
                RT_MSG_FMC_SD_STATUS:    "sd",
                RT_MSG_FMC_RADIO_STATUS: "radio",
            }[msg]
            with self._lock:
                self._fas_fmc.setdefault(key, {})[field] = d

        elif msg in (RT_MSG_PMB_PWR, RT_MSG_PMB_VMON,
                     RT_MSG_PMB_TEMP, RT_MSG_PMB_CHARGER):
            field = {
                RT_MSG_PMB_PWR:     "pwr",
                RT_MSG_PMB_VMON:    "vmon",
                RT_MSG_PMB_TEMP:    "temp",
                RT_MSG_PMB_CHARGER: "charger",
            }[msg]
            with self._lock:
                self._fas_pmb.setdefault(key, {})[field] = d

        elif msg == RT_MSG_IMC_STATUS:
            with self._lock:
                self._fas_imc.update({
                    "board_id":    board_id,
                    "armed":       bool(d.get("armed")),
                    "arm_line":    bool(d.get("arm_line")),
                    "disarm_line": bool(d.get("disarm_line")),
                    "flags":       d.get("flags", 0),
                })
            self._log(2, f"[bridge] IMC status board={board_id} armed={bool(d.get('armed'))}")

    # ── FAS frame send helpers ────────────────────────────────────────────────

    def _next_seq(self) -> int:
        with self._lock:
            s = self._seq
            self._seq = (self._seq + 1) & 0xFF
        return s

    def _send_frame(self, can_id: int, data: bytes) -> None:
        frame = encode_frame(can_id, data)
        try:
            self._serial.write(frame)
        except serial.SerialException as e:
            self._log(0, f"[bridge] serial write error: {e}")

    def _send_discovery_req(self) -> None:
        cid = can_id_pack(RT_MSG_DISCOVERY_REQ, RT_BOARD_GS, 0, 0, 0, self._next_seq())
        self._send_frame(cid, _pad8())

    def _send_pwm_set(self, board_id: int, channel: int,
                      pulse_us: int, period_us: int = 20000) -> None:
        cid     = can_id_pack(RT_MSG_PWM_SET, RT_BOARD_GS, board_id, channel, 0, self._next_seq())
        duty    = pulse_to_q15(pulse_us, period_us)
        payload = _encode_pwm_set(duty, period_us)
        self._send_frame(cid, payload)

    def _send_load_sw_set(self, board_id: int, channel: int,
                          enable: bool, hold_ms: int = 0) -> None:
        cid     = can_id_pack(RT_MSG_LOAD_SW_SET, RT_BOARD_GS, board_id, channel, 0, self._next_seq())
        payload = _encode_load_sw_set(enable, hold_ms)
        self._send_frame(cid, payload)

    def _send_imc_arm(self, board_id: int, pulse_ms: int = 0) -> None:
        cid     = can_id_pack(RT_MSG_IGN_ARM, RT_BOARD_GS, board_id, 0, 0, self._next_seq())
        self._send_frame(cid, _encode_imc_cmd(pulse_ms))

    def _send_imc_disarm(self, board_id: int, pulse_ms: int = 0) -> None:
        cid     = can_id_pack(RT_MSG_IGN_DISARM, RT_BOARD_GS, board_id, 0, 0, self._next_seq())
        self._send_frame(cid, _encode_imc_cmd(pulse_ms))

    # ── Background loops ─────────────────────────────────────────────────────

    def _serial_read_loop(self) -> None:
        self._log(1, f"[bridge] serial reader started on {self._port}")
        while True:
            try:
                chunk = self._serial.read(256)
                if chunk:
                    self._parser.feed(chunk)
            except serial.SerialException as e:
                self._log(0, f"[bridge] serial error: {e}")
                time.sleep(1)

    def _discovery_loop(self) -> None:
        self._log(2, "[bridge] discovery loop started")
        while True:
            self._send_discovery_req()
            # Mark boards offline if not heard from in BOARD_TIMEOUT_S
            now = time.monotonic()
            with self._lock:
                for key, info in self._fas_boards.items():
                    if info["online"] and (now - info.get("last_seen", now)) > BOARD_TIMEOUT_S:
                        info["online"] = False
                        self._log(1, f"[bridge] board {key} timed out → offline")
            time.sleep(DISCOVERY_INTERVAL_S)

    def _publish_loop(self) -> None:
        self._log(2, "[bridge] telemetry publisher started")
        while True:
            now_ms = int(time.monotonic() * 1000)

            with self._lock:
                # Drain the ADC sample queue and collapse to latest per (board_id, channel).
                # Node string derived from board key ("EPB:0" → "EPB_1", 1-based).
                latest: dict[tuple[int, int], dict] = {}
                for s in self._adc_samples:
                    bid = s["board_id"]
                    for ch, v_key in ((0, "v0"), (1, "v1")):
                        latest[(bid, ch)] = {
                            "node":      self._board_node(bid),
                            "channel":   ch,
                            "value":     s[v_key],
                            "timestamp": s["ts_ms"],
                        }
                self._adc_samples.clear()

                fas_boards_snap = [
                    {"key": k, **{kk: vv for kk, vv in v.items() if kk != "last_seen"}}
                    for k, v in self._fas_boards.items()
                ]
                fas_actuators_snap = {
                    k: [by_ch[c] for c in sorted(by_ch)]
                    for k, by_ch in self._fas_actuators.items()
                }
                fas_sensors_snap = {k: dict(v) for k, v in self._fas_sensors.items()}
                fas_board_status_snap = {k: dict(v) for k, v in self._fas_board_status.items()}
                fas_fmc_snap = {k: dict(v) for k, v in self._fas_fmc.items()}
                fas_pmb_snap = {k: dict(v) for k, v in self._fas_pmb.items()}
                fas_imc_snap = dict(self._fas_imc)

            fas_sensor_payload = {
                "source":  "FAS",
                "sensors": list(latest.values()),
            }
            engine_payload = {
                "source":  self._node_id,
                "sensors": list(latest.values()),
            }
            flight_payload = {
                "source":           self._node_id,
                "fas_boards":       fas_boards_snap,
                "fas_actuators":    fas_actuators_snap,
                "fas_sensors":      fas_sensors_snap,
                "fas_board_status": fas_board_status_snap,
                "fas_fmc":          fas_fmc_snap,
                "fas_pmb":          fas_pmb_snap,
                "fas_imc":          fas_imc_snap,
            }
            #self._client.publish(TELEMETRY_TOPIC, json.dumps(engine_payload),    qos=0)
            if latest:
                self._client.publish(TELEMETRY_TOPIC, json.dumps(engine_payload), qos=0)
            self._client.publish(FLIGHT_TOPIC,    json.dumps(flight_payload), qos=0)

            time.sleep(self._publish_ms / 1000.0)

    def _board_node(self, board_id: int) -> str:
        """Return the node string for a board_id, e.g. board_id=0 → 'EPB_1'."""
        for key in self._fas_boards:
            colon = key.find(":")
            if colon != -1 and int(key[colon + 1:]) == board_id:
                return key[:colon] + "_" + str(board_id)
        return f"EPB_{board_id}"

    def _log(self, level: int, msg: str) -> None:
        if self._verbosity >= level:
            print(msg, flush=True)

    def run(self) -> None:
        self._log(1, f"[bridge] FAS bridge starting: {self._port} @ {self._baud} baud")

        for target in (self._serial_read_loop, self._discovery_loop, self._publish_loop):
            threading.Thread(target=target, daemon=True).start()

        self._client.loop_forever()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="FAS RS-422 to MQTT bridge — runs in place of novaGround's FAS integration",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--port",       required=True,
                   help="Serial port connected to the FAS FMC bridge (e.g. /dev/ttyUSB0)")
    p.add_argument("--baud",       type=int, default=460800)
    p.add_argument("--broker",     default="localhost:1883")
    p.add_argument("--node-id",    default="FAS",
                   help="MQTT client ID and telemetry source name")
    p.add_argument("--publish-ms", type=int, default=50,
                   help="Telemetry publish interval ms")
    p.add_argument("--verbosity",  type=int, default=1, choices=[0, 1, 2])
    args = p.parse_args()

    FasBridge(
        port=args.port,
        baud=args.baud,
        broker=args.broker,
        node_id=args.node_id,
        publish_ms=args.publish_ms,
        verbosity=args.verbosity,
    ).run()


if __name__ == "__main__":
    main()
