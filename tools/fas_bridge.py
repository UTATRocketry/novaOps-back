#!/usr/bin/env python3
"""
fas_bridge.py  —  Direct FAS RS-422 to MQTT bridge.

Runs on any machine with a serial port connected to the FAS FMC bridge.
Publishes nova/telemetry/engine and nova/telemetry/flight in the same format
as novaGround so the novaOps backend sees no difference. Translates inbound
nova/command FAS commands to wire frames. All non-FAS command types are
silently dropped.

Telemetry published
  nova/telemetry/engine — sensors: dict keyed "node:channel" (e.g. "EPB_0:0"),
                          each {node, channel, value, timestamp}, updated in
                          place so order is stable and timestamp only advances
                          on a fresh ADC sample
  nova/telemetry/flight — full board state snapshot under "data", mirroring the
                          gs server. data contains:
                          fas_boards:       online/uptime/fw/caps per board
                          fas_actuators:    per-channel actuator state (EPB)
                          fas_sensors:      SENSOR_STATUS masks per board
                          fas_board_status: EPB bus voltage / current rails
                          fas_fmc:          FMC IMU/baro/GPS/temp/SD/radio
                          fas_pmb:          PMB power/vmon/temp/charger
                          fas_imc:          IMC arm/disarm state
  All message types in gs/protocol.py are decoded via decode_payload().

Inbound commands handled (on nova/command)
  {"type":"fas",     ...}   — both op-shape and board_type/port/action shape.
       op-shape ops: pwm_set, load_sw_set, failsafe, imc_arm, imc_disarm,
       discover, actuator_query, buzzer (FMC buzzer; a "notes" array streams a
       whole melody as BEGIN/NOTE.../PLAY), sd_cmd (FMC SD logger rate / clear).
  {"type":"console", ...}   — two-way console control + TX:
       action "start"/"stop"  toggle RX frame streaming to nova/console
       action "list_ports"    enumerate serial ports → nova/console
       action "configure"     switch serial port/baud and reconnect
       action "tx"            encode a packet (op/fields/raw) and write to FAS
  {"type":"data_file", ...} — record ADC samples to a CSV in --data-dir:
       action "start_data_saving" (filename) / "stop_data_saving"
  everything else           — silently dropped

Console output published (on nova/console)
  {"type":"fas_frame",     "dir":"rx", ...}  decoded inbound frames while active
  {"type":"console_tx",    "ok":..., "frame_hex":...}  echo of a sent packet
  {"type":"console_ports", "ports":[...]}    available serial ports
  {"type":"console_config","ok":..., "port":..., "baud":...}  reconfigure result
  {"type":"console_status","active":bool}    start/stop acknowledgement

Dependencies: pip install paho-mqtt pyserial

Usage:
    python fas_bridge.py --port /dev/ttyUSB0 [--baud 460800]
                         [--broker localhost:1883] [--node-id FAS]
                         [--publish-ms 50] [--verbosity 0|1|2]
                         [--imc-board-id N]
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import threading
import time
from enum import IntEnum

import paho.mqtt.client as mqtt
import serial
import serial.tools.list_ports

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
RT_MSG_FMC_BUZZER         = 0x36   # GS → FMC: melody note stream
RT_MSG_RECOVERY_ARM       = 0x37
RT_MSG_RECOVERY_DEPLOY    = 0x38
RT_MSG_PMB_CHARGER        = 0x39
RT_MSG_PMB_CHG_EN         = 0x3A   # GS → PMB: allow / suspend charging
RT_MSG_FMC_SD_CMD         = 0x3B   # GS → FMC: set SD log rate / clear card
RT_MSG_DEBUG_LOG          = 0x3E
RT_MSG_ACK                = 0x3F

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

# ---- FMC onboard-sensor scaling -------------------------------------------
#
# The FMC sends each onboard sensor as a compact int (see rt_proto.h). These
# factors turn the wire value into the engineering unit the UI charts, exactly
# mirroring how ADC codes become volts/mA above. Datasheet-confirmed:
#   LSM6DSOX accel +/-2 g    to 0.061 mg/LSB  = 0.000061 g/LSB
#   LSM6DSOX gyro  +/-250dps to 8.75 mdps/LSB = 0.00875 dps/LSB
#   ADXL375 high-g sent as centi-g; MMC5983MA sent as milligauss.
IMU_ACCEL_LSB_TO_G = 0.000061
IMU_GYRO_LSB_TO_DPS = 0.00875
ACCEL_HG_CENTI_TO_G = 0.01
MAG_MILLIGAUSS_TO_UT = 0.1          # 1 mG = 0.1 uT
BARO_SEA_LEVEL_PA = 101325.0


def _fmc_altitude_m(pressure_pa: float) -> float:
    """Standard ISA barometric altitude from absolute pressure (pascals)."""
    if pressure_pa <= 0:
        return 0.0
    return 44330.0 * (1.0 - (pressure_pa / BARO_SEA_LEVEL_PA) ** 0.190295)


# Charger flag bits (mirror RT_CHG_FLAG_*)
CHG_FLAG_PRESENT = 1 << 0
CHG_FLAG_ENABLED = 1 << 1
CHG_FLAG_VIN_GOOD = 1 << 2
CHG_FLAG_CHARGING = 1 << 3

# Compacted LTC4162 state / status codes (set in pmb charger.c)
CHARGER_STATE_NAMES = {
    0: "off", 1: "bat-detect", 2: "suspended", 3: "precharge", 4: "CC/CV",
    5: "ntc-pause", 6: "timer-term", 7: "full", 8: "max-time-fault",
    9: "bat-missing", 10: "bat-short",
}
CHARGE_STATUS_NAMES = {
    0: "off", 1: "ilim", 2: "thermal", 3: "vin-uvcl", 4: "iin-limit",
    5: "const-current", 6: "const-voltage",
}

# PMB status flag bits (mirror RT_PMB_FLAG_*)
PMB_FLAG_BUCK_ON = 1 << 0
PMB_FLAG_BOOST_ON = 1 << 1
PMB_FLAG_PG_3V3 = 1 << 2
PMB_FLAG_PG_8V4 = 1 << 3
PMB_FLAG_PG_24V0 = 1 << 4
PMB_FLAG_CHARGER = 1 << 5
PMB_FLAG_BATT_SRC = 1 << 6

# SD logger states (mirror RT_SD_STATE_*)
SD_STATE_NAMES = {0: "absent", 1: "no-fs", 2: "mounted", 3: "logging", 4: "error"}

# SD status flag bits (mirror RT_SD_FLAG_*)
SD_FLAG_LOGGING      = 1 << 0
SD_FLAG_NEAR_FULL    = 1 << 1
SD_FLAG_FULL         = 1 << 2
SD_FLAG_RATE_REDUCED = 1 << 3
SD_FLAG_STALLED      = 1 << 4
# Active decimation, reported in the top 3 bits of `flags` (mirror RT_SD_RATE_*).
# code = index into SD_RATE_DIVS (0 = full); a code past the table = custom divisor.
SD_RATE_SHIFT = 5
SD_RATE_DIVS  = [1, 5, 10, 50, 100]

# SD command ops (mirror RT_SD_CMD_*)
RT_SD_CMD_SET_RATE = 0    # arg = 1..255 decimation divisor
RT_SD_CMD_CLEAR    = 1    # reformat / clear the card

# RFD900x status flag bits (mirror RT_RFD_FLAG_*)
RFD_FLAG_POWERED = 1 << 0
RFD_FLAG_ENABLED = 1 << 1

# Buzzer command ops (mirror RT_BUZZER_OP_*)
BUZZER_OP_BEGIN = 0
BUZZER_OP_NOTE = 1
BUZZER_OP_PLAY = 2
BUZZER_OP_STOP = 3

# Heartbeat timeout matching loops.cpp kBoardTimeoutS
BOARD_TIMEOUT_S   = 3.0
DISCOVERY_INTERVAL_S = 2.0
# Drop an engine sensor from the published set if no fresh ADC sample has
# arrived within this many seconds, so the bridge stops republishing values
# for boards that have gone away.
ENGINE_STALE_S    = 3.0

# ── Flight FSM taxonomy ───────────────────────────────────────────────────────
# Kept in sync with tools/novaMock.py. fas_state is the top-level avionics state;
# flight_phase is the finer phase reported alongside it. FLIGHT_EVENTS maps each
# event name to (id, default severity); events are published on nova/console as
# {"type":"flight_event","event":{id,name,severity}}.
FAS_STATES = ["INIT", "STANDBY", "ARMED", "IN_FLIGHT", "AWAITING_RECOVERY"]

FLIGHT_PHASES = [
    "PAD", "LIFTOFF", "POWERED_ASCENT", "COASTING", "APOGEE",
    "DROGUE_DESCENT", "MAIN_DESCENT", "BALLISTIC_DESCENT", "LANDED",
]

EVENT_SEVERITY = ["DEBUG", "INFO", "WARNING", "ERROR", "FATAL"]

FLIGHT_EVENTS = {
    "ARMING_DETECTED":  (10, "INFO"),
    "LAUNCH_DETECTED":  (11, "INFO"),
    "BURNOUT_DETECTED": (12, "INFO"),
    "APOGEE_DETECTED":  (13, "INFO"),
    "DROGUE_DEPLOYED":  (14, "INFO"),
    "MAIN_DEPLOYED":    (15, "INFO"),
    "IMPACT_DETECTED":  (16, "WARNING"),
}

_PHASE_EVENT = {
    "LIFTOFF":        "LAUNCH_DETECTED",
    "COASTING":       "BURNOUT_DETECTED",
    "APOGEE":         "APOGEE_DETECTED",
    "DROGUE_DESCENT": "DROGUE_DEPLOYED",
    "MAIN_DESCENT":   "MAIN_DEPLOYED",
    "LANDED":         "IMPACT_DETECTED",
}

# Altitude AGL (m) at/below which the main chute is expected; descent faster than
# the ballistic threshold (m/s) with no chute is flagged ballistic.
MAIN_DEPLOY_ALT_M  = 450.0
BALLISTIC_SPEED_MS = 75.0


class FlightFsm:
    """Derives fas_state + flight_phase from FMC baro altitude and IMC arm, and
    emits flight events on transitions.

    The FMC firmware is the real authority for flight state; until it sends that
    over the wire, the bridge infers it from barometric altitude so the frontend
    still gets a phase/state and events. Feed ``update(alt_m, dt_s)`` each cycle
    (alt may be None when no baro yet) and ``set_armed`` from IMC status; collect
    queued events with ``drain``."""

    def __init__(self) -> None:
        self.phase = "PAD"
        self.armed = False
        self.ground_alt: float | None = None
        self.max_alt = 0.0
        self._last_alt: float | None = None
        self._vvel = 0.0
        self._last_vvel = 0.0
        self._vacc = 0.0
        self._events = []

    def _fire(self, name: str, severity: str | None = None) -> None:
        eid, default_sev = FLIGHT_EVENTS[name]
        self._events.append({"id": eid, "name": name,
                             "severity": severity or default_sev})

    def drain(self) -> list:
        out = self._events
        self._events = []
        return out

    def fas_state(self) -> str:
        if self.phase == "PAD":
            return "ARMED" if self.armed else "STANDBY"
        if self.phase == "LANDED":
            return "AWAITING_RECOVERY"
        return "IN_FLIGHT"

    def set_armed(self, armed: bool) -> None:
        if armed and not self.armed and self.phase == "PAD":
            self._fire("ARMING_DETECTED")
        self.armed = bool(armed)

    def _transition(self, new_phase: str) -> None:
        if new_phase == self.phase or new_phase not in FLIGHT_PHASES:
            return
        self.phase = new_phase
        ev = _PHASE_EVENT.get(new_phase)
        if ev:
            self._fire(ev)

    def update(self, alt: float | None, dt: float) -> None:
        if alt is None or dt <= 0:
            return
        if self.ground_alt is None:
            self.ground_alt = alt
        # Smoothed vertical velocity / acceleration from successive baro samples.
        if self._last_alt is not None:
            v = (alt - self._last_alt) / dt
            self._vvel = 0.7 * self._vvel + 0.3 * v
            a = (self._vvel - self._last_vvel) / dt
            self._vacc = 0.7 * self._vacc + 0.3 * a
            self._last_vvel = self._vvel
        self._last_alt = alt
        agl = alt - self.ground_alt
        self.max_alt = max(self.max_alt, agl)

        p = self.phase
        if p == "PAD":
            if agl > 3.0 or self._vvel > 3.0:
                self._transition("LIFTOFF")
        elif p == "LIFTOFF":
            self._transition("POWERED_ASCENT")
        elif p == "POWERED_ASCENT":
            if agl > 50.0 and self._vacc <= 0.0:        # motor burnout
                self._transition("COASTING")
        elif p == "COASTING":
            if self.max_alt > 50.0 and self._vvel <= 0.0:
                self._transition("APOGEE")
        elif p == "APOGEE":
            self._transition("DROGUE_DESCENT")
        elif p == "DROGUE_DESCENT":
            if agl <= MAIN_DEPLOY_ALT_M:
                self._transition("MAIN_DESCENT")
            elif self._vvel < -BALLISTIC_SPEED_MS:
                self._transition("BALLISTIC_DESCENT")
        elif p == "BALLISTIC_DESCENT":
            if agl <= MAIN_DEPLOY_ALT_M:
                self._transition("MAIN_DESCENT")
        elif p == "MAIN_DESCENT":
            if agl <= 2.0 and abs(self._vvel) < 2.0:
                self._transition("LANDED")


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
        if msg == RT_MSG_FMC_IMU_ACCEL:
            scale = IMU_ACCEL_LSB_TO_G; unit = "g"; decimals = 5
        elif msg == RT_MSG_FMC_IMU_GYRO:
            scale = IMU_GYRO_LSB_TO_DPS; unit = "dps";  decimals = 4
        elif msg == RT_MSG_FMC_ACCEL_HG:
            scale = ACCEL_HG_CENTI_TO_G; unit = "g";  decimals = 5
        elif msg == RT_MSG_FMC_MAG:
            scale = MAG_MILLIGAUSS_TO_UT; unit = "uT";  decimals = 3
        x, y, z, t_ms = struct.unpack_from("<3hH", data, 0)
        return {"raw_axes": [x, y, z], "unit": unit, "axes": [round(v * scale, decimals) for v in [x, y, z]], "t_ms": t_ms}
    if msg == RT_MSG_FMC_BARO and fits("<ih"):
        pressure_pa, temp_cc = struct.unpack_from("<ih", data, 0)
        return {"pressure_pa": pressure_pa, "pressure_hpa": round(pressure_pa / 100.0, 3), 
                "temp_cc": temp_cc, "temp_c": round(temp_cc / 100.0, 2),
                "altitude_m": round(_fmc_altitude_m(pressure_pa), 2)}
    if msg == RT_MSG_FMC_GPS_POS and fits("<ii"):
        lat_1e7, lon_1e7 = struct.unpack_from("<ii", data, 0)
        return {"lat_1e7": lat_1e7, "lon_1e7": lon_1e7,
                "lat": round(lat_1e7 * 1e-7, 7), "lon": round(lon_1e7 * 1e-7, 7)
                }
    if msg == RT_MSG_FMC_GPS_INFO and fits("<hBBHH"):
        alt_m, fix, sats, hdop_x10, speed_cms = struct.unpack_from("<hBBHH", data, 0)
        return {"alt_m": alt_m, "fix": fix, "sats": sats,
                "hdop": round(hdop_x10 / 10.0, 1),"speed_mps": round(speed_cms / 100.0, 2)}
    if msg == RT_MSG_FMC_HEALTH and fits("<BBBBHBB"):
        imu_id, accel_id, mag_id, present_mask, baro_c1, gfix, gsats = struct.unpack_from("<BBBBHBB", data, 0)
        return {"imu_ok": imu_id == 0x6C,
                "accel_ok":accel_id == 0xE5,
                "mag_ok": mag_id == 0x30,
                "baro_ok": baro_c1 not in (0x0000, 0xFFFF),
                "gps_present": bool(present_mask & 0x10),}
    
    if msg == RT_MSG_PMB_PWR and fits("<HHHH"):
        v8_mv, i8_ma, v24_mv, i24_ma = struct.unpack_from("<HHHH", data, 0)
        return {
                "v_8v4": round(v8_mv / 1000.0, 3),
                "i_8v4": round(i8_ma / 1000.0, 3),
                "v_24v0": round(v24_mv / 1000.0, 3),
                "i_24v0": round(i24_ma / 1000.0, 3),
                "p_8v4": round(v8_mv * i8_ma / 1e6, 2),
                "p_24v0": round(v24_mv * i24_ma / 1e6, 2),}
    if msg == RT_MSG_PMB_VMON and fits("<HHHBB"):
        vmain_mv, vbatt_mv, vgse_mv, flags, _ = struct.unpack_from("<HHHBB", data, 0)
        return {
            "v_main": round(vmain_mv / 1000.0, 3),
            "v_batt": round(vbatt_mv / 1000.0, 3),
            "v_gse": round(vgse_mv / 1000.0, 3),
            "buck_on": bool(flags & 0x01), "boost_on": bool(flags & 0x02),
            "pg_3v3": bool(flags & 0x04), "pg_8v4": bool(flags & 0x08),
            "pg_24v0": bool(flags & 0x10), "charger": bool(flags & 0x20),
            "batt_src": bool(flags & 0x40),
        }
    if msg == RT_MSG_PMB_TEMP and fits("<hhhH"):
        temp_amb_cc, temp_buck_cc, temp_boost_cc, _ = struct.unpack_from("<hhhH", data, 0)
        def _t(cc):
                return None if cc == 0x7FFF else round(cc / 100.0, 2)
        return {"temp_amb": _t(temp_amb_cc), "temp_buck": _t(temp_buck_cc), "temp_boost": _t(temp_boost_cc)}
    if msg == RT_MSG_PMB_CHARGER and fits("<hHBBBB"):
        i_chg_ma, v_bat_mv, flags, state, status, cells = struct.unpack_from("<hHBBBB", data, 0)
        return {
                "i_chg_a": round(i_chg_ma/ 1000.0, 3),
                "v_bat": round(v_bat_mv / 1000.0, 3),
                "present": bool(flags & CHG_FLAG_PRESENT),
                "enabled": bool(flags & CHG_FLAG_ENABLED),
                "vin_good": bool(flags & CHG_FLAG_VIN_GOOD),
                "charging": bool(flags & CHG_FLAG_CHARGING),
                "state": CHARGER_STATE_NAMES.get(state, "?"),
                "status": CHARGE_STATUS_NAMES.get(status, "?"),
                "cells": cells,
            }
    if msg == RT_MSG_BOARD_STATUS and fits("<HHHH"):
        vmon_8v4_mv, vmon_24v_mv, isense_8v4_ma, isense_24v_ma = struct.unpack_from("<HHHH", data, 0)
        return {
                "i_8v4": round(isense_8v4_ma / 1000.0, 3),
                "i_24v0": round(isense_24v_ma / 1000.0, 3),
                "v_8v4": round(vmon_8v4_mv / 1000.0, 3),
                "v_24v0": round(vmon_24v_mv / 1000.0, 3),
            }
    if msg == RT_MSG_FMC_TEMP and fits("<hhI"):
        t_h7_cc, t_pwr_cc, _ = struct.unpack_from("<hhI", data, 0)
        def _ft(cc):
            return None if cc == 0x7FFF else round(cc / 100.0, 2)
        return {"temp_h7": _ft(t_h7_cc), "temp_pwr": _ft(t_pwr_cc)}
    if msg == RT_MSG_FMC_SD_STATUS and fits("<BBBBHH"):
        state, err, pct_used, flags, free_mb, total_mb = struct.unpack_from("<BBBBHH", data, 0)
        rate_code = (flags >> SD_RATE_SHIFT) & 0x07
        rate_div  = SD_RATE_DIVS[rate_code] if rate_code < len(SD_RATE_DIVS) else None
        return {"state": state, "state_name": SD_STATE_NAMES.get(state, "?"),
                "err": err, "pct_used": pct_used,
                "free_mb": free_mb, "total_mb": total_mb,
                "logging": bool(flags & SD_FLAG_LOGGING),
                "near_full": bool(flags & SD_FLAG_NEAR_FULL),
                "full": bool(flags & SD_FLAG_FULL),
                "rate_reduced": bool(flags & SD_FLAG_RATE_REDUCED),
                "stalled": bool(flags & SD_FLAG_STALLED),
                "rate_div": rate_div}
    if msg == RT_MSG_DEBUG_LOG:
        return {"text": data.rstrip(b"\x00").decode("ascii", errors="replace")}
    if msg == RT_MSG_FMC_RADIO_STATUS and fits("<BBHI"):
        flags, every_n, tx_frames, tx_bytes = struct.unpack_from("<BBHI", data, 0)
        return {"powered": bool(flags & RFD_FLAG_POWERED),
                "enabled": bool(flags & RFD_FLAG_ENABLED),
                "every_n": every_n,
                "tx_frames": tx_frames, 
                "tx_bytes": tx_bytes
            }
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


def _encode_buzzer(op: int, idx: int, freq_hz: int, dur_ms: int, vol: int) -> bytes:
    # rt_fmc_buzzer_t: u8 op, u8 idx, u16 freq_hz, u16 dur_ms, u16 vol (0..255)
    return struct.pack("<BBHHH", op & 0xFF, idx & 0xFF,
                       freq_hz & 0xFFFF, dur_ms & 0xFFFF, vol & 0xFFFF)


def _encode_sd_cmd(op: int, arg: int) -> bytes:
    # rt_fmc_sd_cmd_t: u8 op, u8 arg, u16 reserved, u32 reserved
    return struct.pack("<BBHI", op & 0xFF, arg & 0xFF, 0, 0)


def _pad8() -> bytes:
    return b"\x00" * 8


def pulse_to_q15(pulse_us: int, period_us: int = 20000) -> int:
    p = period_us if period_us > 0 else 20000
    return min(32767, (pulse_us * 32767) // p)


# ── Op-shape → wire frame encoder ────────────────────────────────────────────

def op_to_frame(op: str, cmd: dict, seq: int) -> tuple[int, bytes] | None:
    """Convert an op-shape command into a (can_id, data) wire frame.

    Shared by the normal fas-op command path and the two-way console TX path so
    both produce identical frames. Returns None for an unknown op.
    """
    board_id = int(cmd.get("board_id", 0))
    channel  = int(cmd.get("channel", 0))

    if op == "pwm_set":
        pulse_us  = int(cmd.get("pulse_us", 0))
        period_us = int(cmd.get("period_us", 20000))
        cid  = can_id_pack(RT_MSG_PWM_SET, RT_BOARD_GS, board_id, channel, 0, seq)
        return cid, _encode_pwm_set(pulse_to_q15(pulse_us, period_us), period_us)
    if op == "load_sw_set":
        enable  = bool(cmd.get("enable", False))
        hold_ms = int(cmd.get("hold_ms", 0))
        cid = can_id_pack(RT_MSG_LOAD_SW_SET, RT_BOARD_GS, board_id, channel, 0, seq)
        return cid, _encode_load_sw_set(enable, hold_ms)
    if op == "failsafe":
        cid = can_id_pack(RT_MSG_ACTUATOR_FAILSAFE, RT_BOARD_GS, board_id, 0, 0, seq)
        return cid, _pad8()
    if op == "imc_arm":
        cid = can_id_pack(RT_MSG_IGN_ARM, RT_BOARD_GS, board_id, 0, 0, seq)
        return cid, _encode_imc_cmd(int(cmd.get("pulse_ms", 0)))
    if op == "imc_disarm":
        cid = can_id_pack(RT_MSG_IGN_DISARM, RT_BOARD_GS, board_id, 0, 0, seq)
        return cid, _encode_imc_cmd(int(cmd.get("pulse_ms", 0)))
    if op == "discover":
        cid = can_id_pack(RT_MSG_DISCOVERY_REQ, RT_BOARD_GS, 0, 0, 0, seq)
        return cid, _pad8()
    if op == "actuator_query":
        cid = can_id_pack(RT_MSG_ACTUATOR_QUERY, RT_BOARD_GS, board_id, channel, 0, seq)
        return cid, _pad8()
    if op == "buzzer":
        # Single low-level FMC buzzer frame. action selects the melody opcode:
        #   begin — reset the note buffer; note — append one (freq,dur,vol) note;
        #   play  — play the buffered melody; stop — silence immediately.
        # The full note-list path is _cmd_fas_buzzer (begin/note.../play) so a
        # melody plays without each note traversing the whole software stack.
        sub = str(cmd.get("action", "play")).lower()
        bop = {"begin": BUZZER_OP_BEGIN, "note": BUZZER_OP_NOTE,
               "play": BUZZER_OP_PLAY, "stop": BUZZER_OP_STOP}.get(sub)
        if bop is None:
            return None
        cid = can_id_pack(RT_MSG_FMC_BUZZER, RT_BOARD_FMC, board_id, channel, 0, seq)
        return cid, _encode_buzzer(bop, int(cmd.get("idx", 0)),
                                   int(cmd.get("freq_hz", 0)),
                                   int(cmd.get("dur_ms", 0)),
                                   int(cmd.get("vol", 255)))
    if op == "sd_cmd":
        # Control the FMC SD logger. action "set_rate" sets the decimation
        # divisor (arg/divisor = 1..255, 1 = full rate); action "clear"
        # reformats the card.
        sub = str(cmd.get("action", "set_rate")).lower()
        if sub == "clear":
            sd_op, arg = RT_SD_CMD_CLEAR, 0
        elif sub == "set_rate":
            sd_op = RT_SD_CMD_SET_RATE
            arg = int(cmd.get("arg", cmd.get("divisor", 1)))
        else:
            return None
        cid = can_id_pack(RT_MSG_FMC_SD_CMD, RT_BOARD_FMC, board_id, channel, 0, seq)
        return cid, _encode_sd_cmd(sd_op, arg)
    return None


def console_packet_to_frame(pkt: dict, seq: int) -> tuple[int, bytes] | None:
    """Convert a console TX packet into a (can_id, data) wire frame.

    Three accepted shapes, tried in order:
      1. op shape — {"op": "pwm_set", "board_id": .., "channel": .., ...}
      2. fields   — {"msg": 0x10, "kind": 0, "board_id": .., "channel": ..,
                     "flags": 0, "data_hex": "aabb..."}  (seq auto-filled)
      3. raw      — {"can_id": 12345678, "data_hex": "aabb.."}  (full 29-bit id)
    Returns None if the packet can't be encoded. Data is right-padded with zeros
    to 8 bytes and truncated to 8 bytes to match the CAN payload limit.
    """
    def parse_data() -> bytes:
        hexstr = str(pkt.get("data_hex", "")).replace(" ", "")
        raw = bytes.fromhex(hexstr) if hexstr else b""
        return (raw + _pad8())[:8]

    if "op" in pkt:
        return op_to_frame(str(pkt["op"]), pkt, seq)
    if "can_id" in pkt:
        return int(pkt["can_id"]) & 0x1FFFFFFF, parse_data()
    if "msg" in pkt:
        cid = can_id_pack(int(pkt.get("msg", 0)), int(pkt.get("kind", RT_BOARD_GS)),
                          int(pkt.get("board_id", 0)), int(pkt.get("channel", 0)),
                          int(pkt.get("flags", 0)), seq)
        return cid, parse_data()
    return None


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
            return max(0, int(parts[1]))
    return 0


# ─────────────────────────────────────────────────────────────────────────────
class FasBridge:
    def __init__(self, port: str, baud: int, broker: str,
                 node_id: str, publish_ms: int, verbosity: int,
                 imc_board_id: int | None = None, data_dir: str = "data") -> None:
        self._port       = port
        self._baud       = baud
        self._node_id    = node_id
        self._publish_ms = publish_ms
        self._verbosity  = verbosity
        # When set, only IMC_STATUS frames from this board_id update _fas_imc.
        # None means accept IMC status from any board.
        self._imc_board_id = imc_board_id
        self._lock       = threading.Lock()
        self._seq        = 0

        # Data saving: when recording, ADC samples are appended to a CSV in
        # data_dir. Driven by {"type":"data_file", ...} commands from the backend.
        self._data_dir     = data_dir
        self._record_lock  = threading.Lock()
        self._record_file  = None    # open file handle, or None when not recording

        # Telemetry state — mirrors gs StateModel: boards, actuators, sensors, imc
        # plus FMC/PMB onboard telemetry, all keyed by board key ("EPB:0", etc.)
        # Engine sensor values are kept as a persistent dict keyed "node:channel"
        # and updated in place, so the published order is stable (like flight data)
        # and the timestamp only advances when a fresh ADC sample arrives.
        self._engine_values: dict[str, dict] = {}  # "EPB_0:0" → {node, channel, value, timestamp}
        self._fas_boards: dict[str, dict]  = {}    # key → {online, uptime_ms, last_seen, ...}
        self._fas_actuators: dict[str, dict[int, dict]] = {}  # key → {channel_idx → state}
        self._fas_sensors: dict[str, dict] = {}    # key → SENSOR_STATUS fields
        self._fas_board_status: dict[str, dict] = {}  # key → BOARD_STATUS V+I
        self._fas_fmc: dict[str, dict] = {}        # key → merged FMC sensor fields
        self._fas_pmb: dict[str, dict] = {}        # key → merged PMB telemetry fields
        self._fas_imc  = {"board_id": 0, "armed": False,
                           "arm_line": False, "disarm_line": False}
        # Flight FSM (fas_state + flight_phase) derived from FMC baro + IMC arm.
        self._fsm = FlightFsm()
        self._fsm_last_t = time.monotonic()
        self._console_active = False

        # Serial — port/baud are mutable so the console can reconfigure them.
        self._serial_lock = threading.Lock()
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
            self._cmd_console(cmd)
        elif t == "data_file":
            self._cmd_data_file(cmd)
        # All other types are silently dropped per spec.

    # ── Data saving ───────────────────────────────────────────────────────────

    def _cmd_data_file(self, cmd: dict) -> None:
        """Start/stop recording ADC samples to a CSV in the bridge's data dir.

        Mirrors the backend's data-saving command:
          {"type":"data_file","action":"start_data_saving","filename":"..._data_0"}
          {"type":"data_file","action":"stop_data_saving"}
        """
        action = str(cmd.get("action", "")).lower()

        if action == "start_data_saving":
            filename = str(cmd.get("filename") or "fas_data")
            if not filename.endswith(".csv"):
                filename += ".csv"
            path = os.path.join(self._data_dir, filename)
            try:
                os.makedirs(self._data_dir, exist_ok=True)
                f = open(path, "w", newline="", encoding="utf-8")
                f.write("timestamp_ms,node,channel,value\n")
                f.flush()
            except OSError as e:
                self._log(0, f"[bridge] data saving open failed: {e}")
                return
            with self._record_lock:
                if self._record_file is not None:
                    self._record_file.close()
                self._record_file = f
            self._log(1, f"[bridge] data saving started -> {path}")

        elif action == "stop_data_saving":
            with self._record_lock:
                if self._record_file is not None:
                    self._record_file.close()
                    self._record_file = None
            self._log(1, "[bridge] data saving stopped")

        else:
            self._log(2, f"[bridge] data_file: unknown action {action!r}")

    def _record_samples(self, node: str, ch: list, now_ms: int) -> None:
        """Append one CSV row per channel if a recording is in progress."""
        with self._record_lock:
            f = self._record_file
            if f is None:
                return
            for idx, code in enumerate(ch):
                f.write(f"{now_ms},{node},{idx},{code * ADC_INT16_TO_V}\n")
            f.flush()

    # ── Console (two-way) ─────────────────────────────────────────────────────

    def _cmd_console(self, cmd: dict) -> None:
        """Handle console control + TX. Output (frames, port lists, tx echoes,
        acks) is published to nova/console for the frontend to display.

        Actions:
          start | stop        — toggle RX frame streaming to nova/console
          list_ports          — enumerate serial ports → console_ports message
          configure           — switch serial port/baud and reconnect
          tx                  — encode a packet (op/fields/raw) and write to FAS
        """
        action = str(cmd.get("action", "")).lower()

        if action in ("start", "stop"):
            with self._lock:
                self._console_active = action == "start"
            self._log(1, f"[bridge] console {action}")
            self._publish_console({"type": "console_status", "active": action == "start"})

        elif action == "list_ports":
            ports = [
                {"device": p.device, "name": p.name,
                 "description": p.description, "hwid": p.hwid}
                for p in serial.tools.list_ports.comports()
            ]
            self._log(1, f"[bridge] console list_ports -> {len(ports)} found")
            self._publish_console({"type": "console_ports", "ports": ports})

        elif action == "configure":
            port = str(cmd.get("port", "")) or self._port
            baud = int(cmd.get("baud", self._baud))
            ok, err = self._reconnect_serial(port, baud)
            self._publish_console({
                "type": "console_config",
                "ok": ok, "port": port, "baud": baud,
                **({"error": err} if err else {}),
            })

        elif action == "tx":
            frame = console_packet_to_frame(cmd, self._next_seq())
            if frame is None:
                self._log(2, f"[bridge] console tx: cannot encode {cmd}")
                self._publish_console({"type": "console_tx",
                                       "ok": False, "error": "unencodable packet"})
                return
            can_id, data = frame
            self._send_frame(can_id, data)
            hex_frame = encode_frame(can_id, data).hex()
            decoded   = can_id_unpack(can_id)
            self._log(1, f"[bridge] console tx msg=0x{decoded['msg']:02x} "
                         f"board={decoded['board_id']} ch={decoded['channel']}")
            self._publish_console({
                "type":      "console_tx",
                "ok":        True,
                "can_id":    can_id,
                "msg_type":  decoded["msg"],
                "board_kind": decoded["kind"],
                "board_id":  decoded["board_id"],
                "channel":   decoded["channel"],
                "data_hex":  data.hex(),
                "frame_hex": hex_frame,
            })

        else:
            self._log(2, f"[bridge] console: unknown action {action!r}")

    def _publish_console(self, payload: dict) -> None:
        payload.setdefault("source", "novaGround")
        self._client.publish(CONSOLE_TOPIC, json.dumps(payload), qos=0)

    def _publish_flight_event(self, event: dict) -> None:
        """Publish a flight event as {"type":"flight_event","event":{id,name,
        severity}} on nova/console (rebroadcast verbatim by the backend)."""
        self._publish_console({
            "type": "flight_event",
            "event": {"id": event["id"], "name": event["name"],
                      "severity": event["severity"]},
            "source": self._node_id,
        })
        self._log(1, f"[bridge] flight_event #{event['id']} {event['name']} "
                     f"({event['severity']})")

    def _reconnect_serial(self, port: str, baud: int) -> tuple[bool, str | None]:
        """Open a new serial port and swap it in. The read loop picks up the new
        handle on its next iteration. Returns (ok, error_message)."""
        try:
            new_serial = serial.Serial(port, baud, timeout=1)
        except (serial.SerialException, ValueError, OSError) as e:
            self._log(0, f"[bridge] serial reconfigure failed: {e}")
            return False, str(e)
        with self._serial_lock:
            old = self._serial
            self._serial = new_serial
            self._port = port
            self._baud = baud
        try:
            old.close()
        except Exception:
            pass
        self._log(1, f"[bridge] serial reconfigured -> {port} @ {baud} baud")
        return True, None

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
                         f"pulse={pulse_us}us")

        elif port == "gpio":
            act = action.upper()
            if act == "ARM":
                self._send_imc_arm(board_id, 250)
                self._log(1, f"[bridge] fas imc_arm EPB:{board_id}")
            elif act == "DISARM":
                self._send_imc_disarm(board_id, 250)
                self._log(1, f"[bridge] fas imc_disarm EPB:{board_id}")
            else:
                self._log(2, f"[bridge] fas gpio: unknown action {action!r}")
        else:
            self._log(2, f"[bridge] fas: unknown port {port!r}")

    def _cmd_fas_op(self, cmd: dict) -> None:
        op = str(cmd.get("op", ""))
        # A buzzer op carrying a `notes` array is a full melody: the bridge
        # streams it to the FMC as BEGIN, NOTE×N, PLAY on the spot, so notes do
        # not each pay the cost of a round trip through the whole software stack.
        if op == "buzzer" and "notes" in cmd:
            self._cmd_fas_buzzer(cmd)
            return
        frame = op_to_frame(op, cmd, self._next_seq())
        if frame is None:
            self._log(2, f"[bridge] fas: unknown op {op!r}")
            return
        can_id, data = frame
        self._send_frame(can_id, data)
        self._log(1, f"[bridge] fas op={op} board={cmd.get('board_id', 0)} "
                     f"ch={cmd.get('channel', 0)}")

    def _cmd_fas_buzzer(self, cmd: dict) -> None:
        """Stream a note list to the FMC buzzer as BEGIN, NOTE×N, PLAY.

        notes is a list of [freq_hz, dur_ms] or [freq_hz, dur_ms, vol]; a
        freq_hz of 0 is a rest (silence for dur_ms). Mirrors the gs server's
        _send_melody so the FMC sees an identical frame stream. The whole
        melody is buffered on the FMC and played there, so per-note timing is
        not subject to MQTT/serial latency.
        """
        board_id = int(cmd.get("board_id", 0))
        channel  = int(cmd.get("channel", 0))
        notes    = cmd.get("notes") or []

        def buz(bop: int, idx: int = 0, freq: int = 0,
                dur: int = 0, vol: int = 255) -> None:
            cid = can_id_pack(RT_MSG_FMC_BUZZER, RT_BOARD_FMC,
                              board_id, channel, 0, self._next_seq())
            self._send_frame(cid, _encode_buzzer(bop, idx, freq, dur, vol))

        buz(BUZZER_OP_BEGIN)
        sent = 0
        for note in notes:
            try:
                freq = int(note[0])
                dur  = int(note[1])
                vol  = int(note[2]) if len(note) > 2 else 255
            except (TypeError, IndexError, ValueError):
                continue
            buz(BUZZER_OP_NOTE, sent & 0xFF, freq, dur, vol)
            sent += 1
        buz(BUZZER_OP_PLAY)
        self._log(1, f"[bridge] fas buzzer melody FMC:{board_id} notes={sent}")

    # ── FAS frame receive ────────────────────────────────────────────────────

    def _on_frame(self, can_id: int, data: bytes) -> None:
        cid = can_id_unpack(can_id)
        msg = cid["msg"]

        board_id  = cid["board_id"]
        kind_name = BOARD_KIND_NAMES.get(cid["kind"], "UNK")
        key       = f"{kind_name}:{board_id}"
        now       = time.monotonic()
        d         = decode_payload(msg, data)

        # Console raw-frame output (RX direction)
        with self._lock:
            console = self._console_active
        if console:
            self._publish_console({
                "type":       "fas_frame",
                "dir":        "rx",
                "can_id":     can_id,
                "msg_type":   msg,
                "board_kind": cid["kind"],
                "board_id":   board_id,
                "channel":    cid["channel"],
                "data_hex":   data.hex(),
                "decoded":    d,
            })

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
            # Update the persistent engine dict in place, one entry per channel.
            # Stamped samples carry 2 channels, legacy bursts 4.
            # Node comes from THIS frame's own kind+board_id ("EPB_0"); never look
            # it up by board_id alone — the FMC/GS share board_id 0 with the EPB.
            ch = d.get("ch", [])
            now_ms = int(now * 1000)
            node = f"{kind_name}_{board_id}"
            with self._lock:
                for idx, code in enumerate(ch):
                    self._engine_values[f"{node}:{idx}"] = {
                        "node":      node,
                        "channel":   idx,
                        "value":     code * ADC_INT16_TO_V,
                        "timestamp": now_ms,
                    }
            self._record_samples(node, ch, now_ms)
            self._log(2, f"[bridge] adc {key} ch={ch}")

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
            if self._imc_board_id is not None and board_id != self._imc_board_id:
                self._log(2, f"[bridge] IMC status board={board_id} ignored "
                             f"(listening for board {self._imc_board_id})")
            else:
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
        with self._serial_lock:
            ser = self._serial
        try:
            ser.write(frame)
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
            with self._serial_lock:
                ser = self._serial
            try:
                chunk = ser.read(256)
                if chunk:
                    self._parser.feed(chunk)
            except serial.SerialException as e:
                # A reconfigure may have closed this handle out from under us;
                # loop around and pick up the current handle on the next pass.
                self._log(2, f"[bridge] serial error (reconnecting?): {e}")
                time.sleep(0.2)

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
                        self._log(1, f"[bridge] board {key} timed out -> offline")
            time.sleep(DISCOVERY_INTERVAL_S)

    def _publish_loop(self) -> None:
        self._log(2, "[bridge] telemetry publisher started")
        while True:
            now_ms = int(time.monotonic() * 1000)
            with self._lock:
                # Drop sensors whose ADC samples have stopped arriving so we don't
                # keep republishing frozen values for a board that's gone.
                stale = [k for k, v in self._engine_values.items()
                         if now_ms - v["timestamp"] > ENGINE_STALE_S * 1000]
                for k in stale:
                    del self._engine_values[k]

                # Engine sensors: snapshot the persistent dict, keyed "node:channel"
                # so the published order stays stable across messages.
                engine_snap = {k: dict(v) for k, v in self._engine_values.items()}

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
                # Latest FMC barometric altitude (if any) drives the flight FSM.
                fmc_alt = None
                for k, v in self._fas_fmc.items():
                    if k.startswith("FMC") and isinstance(v.get("baro"), dict):
                        fmc_alt = v["baro"].get("altitude_m")
                        break

            # Advance the flight FSM from IMC arm + baro altitude, then collect
            # any events it raised this cycle. (Only the publish thread touches
            # the FSM, so no extra locking is needed here.)
            t_now = time.monotonic()
            dt = t_now - self._fsm_last_t
            self._fsm_last_t = t_now
            self._fsm.set_armed(fas_imc_snap.get("armed", False))
            self._fsm.update(fmc_alt, dt)
            fas_fsm_snap = {"fas_state": self._fsm.fas_state(),
                            "flight_phase": self._fsm.phase}
            flight_events = self._fsm.drain()

            # Source must be a FAS alias so the backend routes these to FAS
            # sensor bindings; it is independent of --node-id (the MQTT client id).
            engine_payload = {
                "source":  "FAS",
                "sensors": engine_snap,
            }
            flight_payload = {
                "source": self._node_id,
                "data": {
                    "fas_boards":       fas_boards_snap,
                    "fas_actuators":    fas_actuators_snap,
                    "fas_sensors":      fas_sensors_snap,
                    "fas_board_status": fas_board_status_snap,
                    "fas_fmc":          fas_fmc_snap,
                    "fas_pmb":          fas_pmb_snap,
                    "fas_imc":          fas_imc_snap,
                    "fas_fsm":          fas_fsm_snap,
                },
            }
            if engine_snap:
                self._client.publish(TELEMETRY_TOPIC, json.dumps(engine_payload), qos=0)
            self._client.publish(FLIGHT_TOPIC,    json.dumps(flight_payload), qos=0)
            for ev in flight_events:
                self._publish_flight_event(ev)

            time.sleep(self._publish_ms / 1000.0)

    def _log(self, level: int, msg: str) -> None:
        if self._verbosity >= level:
            try:
                print(msg, flush=True)
            except UnicodeEncodeError:
                # Windows consoles are often cp1252; never let a stray non-ASCII
                # character (e.g. from wire data) kill a logging thread.
                print(msg.encode("ascii", "replace").decode("ascii"), flush=True)

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
                   help="MQTT client ID (engine telemetry source is always 'FAS')")
    p.add_argument("--publish-ms", type=int, default=50,
                   help="Telemetry publish interval ms")
    p.add_argument("--verbosity",  type=int, default=1, choices=[0, 1, 2])
    p.add_argument("--imc-board-id", type=int, default=None,
                   help="If set, only update IMC arm state from this board_id; "
                        "IMC_STATUS frames from other boards are ignored "
                        "(default: accept any board)")
    p.add_argument("--data-dir",   default="data",
                   help="Directory for recorded data-saving CSV files")
    args = p.parse_args()

    FasBridge(
        port=args.port,
        baud=args.baud,
        broker=args.broker,
        node_id=args.node_id,
        publish_ms=args.publish_ms,
        verbosity=args.verbosity,
        imc_board_id=args.imc_board_id,
        data_dir=args.data_dir,
    ).run()


if __name__ == "__main__":
    main()
