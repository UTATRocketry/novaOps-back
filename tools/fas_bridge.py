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
                          fas_pmb:          PMB power/vmon/temp/charger/chg_cfg
                          fas_imc:          IMC arm/disarm state
                          fas_rab:          RAB recovery-arming status (A/B)
                          fas_aux:          FMC aux (camera + RF amplifier, GNSS PPS)
                          fas_radio_cfg:    FMC vehicle-radio config read-back
                          fas_sound:        soundboard status + clip directory
  All message types in gs/protocol.py are decoded via decode_payload().

Inbound commands handled (on nova/command)
  {"type":"fas",     ...}   — both op-shape and board_type/port/action shape.
       op-shape ops: pwm_set, load_sw_set, failsafe, imc_arm, imc_disarm,
       discover, actuator_query, buzzer (DEPRECATED FMC buzzer; a "notes" array
       streams a whole melody as BEGIN/NOTE.../PLAY), sd_cmd (FMC SD logger rate /
       clear), rab_arm / rab_disarm (recovery arming board, board_id 0=A/1=B),
       aux_power (radio / RunCam / RF-amplifier rail), runcam_record (RunCam
       record start/stop with an auto-stop timeout), radio_config (the full
       STM32WL vehicle-radio profile, one 88-byte bulk record, wired link only),
       sound (soundboard: play/stop/volume/tone/list/clear — buzzer replacement),
       sound_upload (download the staged clip from the backend's url/path — see
       --ops-url — then stream it to the FMC as BEGIN/DATA/END, reporting
       progress and the outcome on nova/console; a legacy inline data_b64 clip
       is still accepted),
       pmb_charger (enable/suspend battery charging + current/voltage limits).
  {"type":"console", ...}   — two-way console control + TX:
       action "start"/"stop"  toggle RX frame streaming to nova/console
       action "list_ports"    enumerate serial ports → nova/console
       action "configure"     switch serial port/baud and reconnect
       action "disconnect"    close the serial port and stay idle
       action "status"        report the current serial link state
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
  {"type":"console_serial","connected":bool,"port":...,"baud":...,"error":...}
                                             serial link state, published on
                                             every connect/disconnect and on
                                             an explicit "status" request
  {"type":"sound_upload_progress", "upload_id":..., "sent":..., "total":...}
  {"type":"sound_upload_result",   "upload_id":..., "ok":bool, "stage":...,
                                   "error":..., "clip_count":...}

Dependencies: pip install paho-mqtt pyserial

The serial port is optional: with no (or an unavailable) port the bridge still
starts, connects to MQTT and serves console commands, so novaOps can list ports
and pick one at runtime with a console "configure" command. A configured port
that is missing or disappears mid-run is retried in the background.

Usage:
    python fas_bridge.py [--port /dev/ttyUSB0] [--baud 460800]
                         [--broker localhost:1883] [--node-id FAS]
                         [--publish-ms 50] [--verbosity 0|1|2]
                         [--imc-board-id N] [--ops-url http://host:8000]
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import struct
import tempfile
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zlib
import time
from collections import namedtuple
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
RS422_MAX_PAYLOAD = 12          # 4-byte CAN ID + up to 8 data bytes (classic frame)
# The FMC's RS-422 link also carries large-payload "bulk" frames (up to 4-byte
# CAN ID + 256 data bytes) for the soundboard clip list. Same magic/len16/crc16
# framing, only the length ceiling differs — see egse_uart.c FRAME_BULK_MAX.
RS422_BULK_PAYLOAD = 260

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

# --- Protocol update: RAB / FMC aux / RF rate / soundboard / PMB charge cfg ---
# (mirrors shared/protocol/rt_proto.h + gs/protocol.py). RAB rides the FMC's
# RS-422 link; RAB A -> board_id 0, RAB B -> board_id 1.
RT_MSG_RAB_POLL           = 0x05   # FMC -> RAB (addressed): request status
RT_MSG_RAB_STATUS         = 0x06   # RAB -> FMC -> GS: rt_rab_status_t
RT_MSG_RAB_DISARM         = 0x07   # FMC -> RAB (addressed): pulse GPIO_DISARM
RT_MSG_FMC_AUX_POWER      = 0x08   # GS -> FMC: modem / RunCam / RF-PA rail + record
RT_MSG_FMC_AUX_STATUS     = 0x09   # FMC -> GS: camera + amplifier state, GNSS PPS
# 0x0A carried the retired serial-radio rate/power mode. The FMC now ignores it
# and the STM32WL profile lives in RT_MSG_FMC_RADIO_CONFIG, so this ID is
# reserved: sending it is a protocol violation, not a no-op.
RT_MSG_RESERVED_LEGACY_RF_CFG = 0x0A
RT_MSG_FMC_SOUND_CMD      = 0x0B   # GS -> FMC: play/stop/clear/volume/list/tone
RT_MSG_FMC_SOUND_BEGIN    = 0x0C   # GS -> FMC (bulk): start a clip upload
RT_MSG_FMC_SOUND_DATA     = 0x0D   # GS -> FMC (bulk): raw clip bytes
RT_MSG_FMC_SOUND_STATUS   = 0x0E   # FMC -> GS: usage / clip count / playing / busy
RT_MSG_FMC_SOUND_CLIP     = 0x0F   # FMC -> GS (bulk): one per stored clip
RT_MSG_PMB_CHG_CFG        = 0x3C   # PMB -> GS: configured charge limits (read-back)
RT_MSG_FMC_RADIO_CONFIG   = 0x3D   # GS <-> FMC (bulk): full vehicle-radio config

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
PMB_FLAG_PROTECT = 1 << 7   # firmware battery UVLO/OV protect: converters cut

# Charge-config flag bits (mirror RT_CHGCFG_FLAG_*)
CHGCFG_FLAG_VLIMIT = 1 << 0          # firmware charge-voltage cutoff active
CHGCFG_FLAG_ENABLED = 1 << 1         # persisted automatic-charge intent
CHGCFG_FLAG_PERSIST_ERROR = 1 << 2   # runtime state may differ from flash
CHGCFG_FLAG_TARGETS_OK = 1 << 3      # direct I/V targets verified by readback
CHGCFG_FLAG_CONTROL_UNKNOWN = 1 << 4 # LTC gate state cannot be proven

# RAB status flag bits (mirror RT_RAB_FLAG_*)
RAB_FLAG_DUAL         = 1 << 0   # RAB in dual-debug mode (one board answers A+B)
RAB_FLAG_DISAGREE     = 1 << 1   # LEGACY FMC PD8/PD9 read -- deprecated, ignored
RAB_FLAG_FMC_RX       = 1 << 2   # link diag: RAB has RX'd FMC bytes within ~300 ms
RAB_FLAG_ARM_MISMATCH = 1 << 3   # RAB-local: commanded arm state != readback (>~1 s)
RAB_FLAG_ARM_EXPECTED = 1 << 4   # RAB-local: current expected/commanded arm state

# FMC aux-power device selector (mirror RT_AUX_DEV_*). Only RADIO is an FMC
# pin; RUNCAM and RF_PA are EPB load switches the FMC is the single writer for.
# arg16 is read for RUNCAM only, where it bounds the recording this power-up
# starts (see _encode_aux_power).
RT_AUX_DEV_RADIO      = 0
RT_AUX_DEV_RUNCAM     = 1
RT_AUX_DEV_RF_PA      = 2
# Device 3 was a RunCam Device Protocol record start/stop. The FMC has no data
# link to the camera, so the current firmware's handle_aux_power_cmd() has no
# case for it and silently discards the frame. Permanently reserved by
# FMC-Interface-Contract.md section 6.6 - never send it, never reuse the number.
RT_AUX_DEV_RESERVED_LEGACY_RUNCAM_REC = 3

# arg16 sentinel: defer to the FMC's persisted runcam_autostop_s. Zero cannot
# carry this meaning because zero is a real setting ("no timer").
RT_AUX_RUNCAM_AUTOSTOP_DEFAULT = 0xFFFF

# Largest RunCam auto-stop the wire field can carry (mirror gs/protocol.py).
FMC_RF_RUNCAM_AUTOSTOP_MAX_S = 43200

# FMC aux status flag bits (mirror RT_AUX_FLAG_*). The rail states are the EPB's
# own ACTUATOR_STATE echo as the FMC saw it, never the FMC's intent, so a board
# that never answered reads "off" rather than a guess.
AUX_FLAG_RUNCAM_POWERED   = 1 << 0
# Bits 1 and 2 described the camera itself and are permanently reserved; the
# firmware masks them out of every frame it sends. Never read, never reuse.
AUX_FLAG_RESERVED_LEGACY_PRESENT   = 1 << 1
AUX_FLAG_RESERVED_LEGACY_RECORDING = 1 << 2
AUX_FLAG_RUNCAM_AUTOSTOP  = 1 << 3
AUX_FLAG_RF_PA_REQUESTED  = 1 << 4   # operator master enable is set
AUX_FLAG_RF_PA_ON         = 1 << 5
AUX_FLAG_RF_PA_CYCLING    = 1 << 6   # the duty-cycle scheduler is running
AUX_FLAG_RF_PA_INHIBIT    = 1 << 7   # held off: the modem is not ready/good

# Soundboard command ops (mirror RT_SND_OP_*)
SND_OP_STOP     = 0
SND_OP_PLAY     = 1   # arg = clip index
SND_OP_CLEAR    = 2
SND_OP_VOLUME   = 3   # arg = 0..255
SND_OP_LIST     = 4
SND_OP_UL_END   = 5   # arg32 = CRC32
SND_OP_UL_ABORT = 6
SND_OP_TONE     = 7   # arg32 = freq_hz | (ms << 16), 0 = default

# Soundboard status flag bits (mirror RT_SND_FLAG_*)
SND_FLAG_BUSY      = 1 << 0
SND_FLAG_UL_ACTIVE = 1 << 1
SND_FLAG_UL_READY  = 1 << 2
SND_FLAG_TONE      = 1 << 3

# On-flash clip encodings (mirror RT_SND_FMT_*). 0 = legacy IMA-ADPCM.
SND_FMT_IMA_ADPCM = 1
SND_FMT_PCM_S16   = 2

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

# STM32WL vehicle-radio status flag bits (mirror RT_RADIO_STATUS_FLAG_*)
RADIO_STATUS_FLAG_POWER_REQUESTED = 1 << 0
RADIO_STATUS_FLAG_POWERED         = 1 << 1
RADIO_STATUS_FLAG_READY           = 1 << 2
RADIO_STATUS_FLAG_CONFIG_VALID    = 1 << 3
RADIO_STATUS_FLAG_READBACK_MATCH  = 1 << 4
RADIO_STATUS_FLAG_TX_ACTIVE       = 1 << 5
RADIO_STATUS_FLAG_CLOCK_CALIBRATED = 1 << 6
RADIO_STATUS_FLAG_FAULT           = 1 << 7

# Buzzer command ops (mirror RT_BUZZER_OP_*)
BUZZER_OP_BEGIN = 0
BUZZER_OP_NOTE = 1
BUZZER_OP_PLAY = 2
BUZZER_OP_STOP = 3

# Heartbeat timeout matching loops.cpp kBoardTimeoutS
BOARD_TIMEOUT_S   = 3.0
DISCOVERY_INTERVAL_S = 2.0
# How long to wait before retrying a configured serial port that failed to open
# or dropped out (unplugged adapter, port held by another process).
SERIAL_RETRY_S    = 2.0
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
            if 4 <= self._len <= RS422_BULK_PAYLOAD:
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
            "protect": bool(flags & PMB_FLAG_PROTECT),
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
    if msg == RT_MSG_FMC_RADIO_STATUS and fits("<BBBBHH"):
        (flags, state, last_fault, queue_depth,
         tx_accepted, tx_dropped) = struct.unpack_from("<BBBBHH", data, 0)
        return {
            "flags": flags, "state": state, "last_fault": last_fault,
            "queue_depth": queue_depth, "tx_accepted": tx_accepted,
            "tx_dropped": tx_dropped,
            "power_requested": bool(flags & RADIO_STATUS_FLAG_POWER_REQUESTED),
            "powered": bool(flags & RADIO_STATUS_FLAG_POWERED),
            "ready": bool(flags & RADIO_STATUS_FLAG_READY),
            "config_valid": bool(flags & RADIO_STATUS_FLAG_CONFIG_VALID),
            "readback_matches": bool(flags & RADIO_STATUS_FLAG_READBACK_MATCH),
            "tx_active": bool(flags & RADIO_STATUS_FLAG_TX_ACTIVE),
            "clock_calibrated": bool(flags & RADIO_STATUS_FLAG_CLOCK_CALIBRATED),
            "fault": bool(flags & RADIO_STATUS_FLAG_FAULT),
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
    if msg == RT_MSG_RAB_STATUS and fits("<8B"):
        rid, fc, arm, disarm, flags, fc_gpio, disagree, rxlo = struct.unpack_from("<8B", data, 0)
        return {
            "rab_id": rid, "fc_armed": fc, "arm_line": arm, "disarm_line": disarm,
            "flags": flags, "fc_armed_gpio": fc_gpio, "disagree": disagree,
            "fmc_rx": bool(flags & RAB_FLAG_FMC_RX),        # RAB hears the FMC (FMC->RAB up)
            "rx_count8": rxlo,                              # low 8 bits of RAB's FMC-RX byte count
            "arm_mismatch": bool(flags & RAB_FLAG_ARM_MISMATCH),  # RAB-local expected != observed
            "arm_expected": bool(flags & RAB_FLAG_ARM_EXPECTED),  # RAB last commanded ARMED
        }
    if msg == RT_MSG_FMC_AUX_STATUS and fits("<BBHHH"):
        (flags, pps_present, pps_count, pps_age,
         record_s) = struct.unpack_from("<BBHHH", data, 0)
        return {
            "aux_flags": flags,
            # Rail states are the EPB's own ACTUATOR_STATE echo as the FMC saw
            # it, never the FMC's intent, so an unanswered board reads "off"
            # rather than a guess.
            "runcam_powered": bool(flags & AUX_FLAG_RUNCAM_POWERED),
            # runcam_present / runcam_recording deliberately absent: nothing in
            # this system can observe the camera, so bits 1 and 2 are reserved
            # and never emitted (contract 6.6). Publishing them would put a
            # permanently-false "not recording" indicator in front of an
            # operator, which is worse than showing nothing.
            "runcam_autostop": bool(flags & AUX_FLAG_RUNCAM_AUTOSTOP),
            "runcam_record_s": (None if record_s == 0xFFFF else record_s),
            "rf_pa_requested": bool(flags & AUX_FLAG_RF_PA_REQUESTED),
            "rf_pa_on": bool(flags & AUX_FLAG_RF_PA_ON),
            "rf_pa_cycling": bool(flags & AUX_FLAG_RF_PA_CYCLING),
            "rf_pa_inhibited": bool(flags & AUX_FLAG_RF_PA_INHIBIT),
            "pps_present": bool(pps_present),
            "pps_count": pps_count, "pps_age_ms": pps_age,
        }
    if msg == RT_MSG_FMC_RADIO_CONFIG:
        try:
            return unpack_fmc_radio_config(data)
        except ValueError as exc:
            return {"config_decode_error": str(exc), "raw_hex": data.hex()}
    if msg == RT_MSG_PMB_CHG_CFG and fits("<BBBB4x"):
        i_set, v_set, cells, flags = struct.unpack_from("<BBBB4x", data, 0)
        return {"i_setting": i_set, "v_setting": v_set, "cells": cells,
                "flags": flags, "vlimit": bool(flags & CHGCFG_FLAG_VLIMIT),
                "enabled_intent": bool(flags & CHGCFG_FLAG_ENABLED),
                "persist_error": bool(flags & CHGCFG_FLAG_PERSIST_ERROR),
                "targets_ok": bool(flags & CHGCFG_FLAG_TARGETS_OK),
                "control_unknown": bool(flags & CHGCFG_FLAG_CONTROL_UNKNOWN)}
    if msg == RT_MSG_FMC_SOUND_STATUS and fits("<BBBBHH"):
        flags, count, playing, pct, used_kb, cap_kb = struct.unpack_from("<BBBBHH", data, 0)
        return {
            "flags": flags, "clip_count": count,
            "playing_idx": (None if playing == 0xFF else playing),
            "pct": pct, "used_kb": used_kb, "cap_kb": cap_kb,
            "busy": bool(flags & SND_FLAG_BUSY),
            "ul_active": bool(flags & SND_FLAG_UL_ACTIVE),
            "ul_ready": bool(flags & SND_FLAG_UL_READY),
            "tone": bool(flags & SND_FLAG_TONE),
        }
    if msg == RT_MSG_FMC_SOUND_CLIP and fits("<BBHII24s"):
        idx, fmt, _, length, rate, name = struct.unpack_from("<BBHII24s", data, 0)
        return {
            "idx": idx, "format": fmt or SND_FMT_IMA_ADPCM,   # 0 = legacy ADPCM
            "length": length, "sample_rate": rate,
            "name": name.split(b"\x00", 1)[0].decode("ascii", "replace"),
        }
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


def _encode_rab_cmd(pulse_ms: int) -> bytes:
    # rt_rab_cmd_t: u16 pulse_ms, u16 reserved, u32 reserved
    return struct.pack("<HHI", pulse_ms & 0xFFFF, 0, 0)


# ── FMC vehicle-radio configuration (RT_MSG_FMC_RADIO_CONFIG, bulk only) ─────
# An 88-byte FMC-authoritative record: the LoRa link, the callsign, the three
# RF pressure channels, and the RF-chain duty cycle plus its EPB peripheral
# bindings. Ported field-for-field from gs/protocol.py; the two must stay
# byte-identical. Valid only on the wired bulk path — never sent over RF.
FMC_RADIO_CONFIG_VERSION = 3
FMC_RADIO_CONFIG_FMT = "<BBBBIIHHIIIIbBBB3B3BBB16sI4BHHHHHHB7x"
FMC_RADIO_PRESSURE_SLOTS = 3
FMC_RADIO_PRESSURE_UNUSED = 0xFF
FMC_RADIO_PERIPHERAL_UNUSED = 0xFF
FMC_RF_FLAG_DUTY_CYCLE = 1 << 0
FMC_RF_FLAG_RUNCAM_AUTOSTOP = 1 << 1
# Enable, not silence, so a cleared bit is quiet: a blank flash and every record
# written before the bit existed both come up silent.
FMC_RF_FLAG_BOOT_SOUND = 1 << 2
# Powering the camera rail starts a recording; dropping it stops one.
FMC_RF_FLAG_REC_ON_POWER = 1 << 3
# Mirrors of the firmware's validation bounds (radio_config_store.h), so a bad
# value is rejected here instead of becoming a failed transaction.
FMC_RF_CYCLE_PERIOD_MIN_MS = 1000
FMC_RF_CYCLE_PERIOD_MAX_MS = 60000
FMC_RF_WARMUP_MAX_MS = 2000
FMC_RF_TAIL_MAX_MS = 2000
FMC_RF_ON_MAX_MS = 30000
FMC_RADIO_CFG_GET = 0
FMC_RADIO_CFG_SET_SAVE = 1
FMC_RADIO_CFG_REQUEST = 0
FMC_RADIO_CFG_FLAG_PERSISTED = 1 << 0
FMC_RADIO_CFG_FLAG_LINK_READY = 1 << 1
FMC_RADIO_CFG_FLAG_READBACK_MATCH = 1 << 2
FMC_RADIO_CFG_FLAG_PLACEHOLDER_ID = 1 << 3
FMC_RADIO_CFG_STATUS_NAMES = {
    0: "request", 1: "accepted", 2: "applied", 3: "invalid",
    4: "store_error", 5: "link_error", 6: "busy",
}
assert struct.calcsize(FMC_RADIO_CONFIG_FMT) == 88

# Wire order of FMC_RADIO_CONFIG_FMT. Naming the fields keeps the decoder off
# positional indices, which are what silently rot when the record changes shape.
_FmcRadioConfigWire = namedtuple("_FmcRadioConfigWire", (
    "op status version reserved0 transaction_id generation "
    "network_id vehicle_node_id allocation_low_hz allocation_high_hz "
    "lora_frequency_hz lora_bandwidth_hz lora_power_dbm lora_sf lora_cr "
    "lora_preamble_symbols "
    "pressure_board_0 pressure_board_1 pressure_board_2 "
    "pressure_channel_0 pressure_channel_1 pressure_channel_2 "
    "reserved1 flags callsign validation_error "
    "rf_pa_board_id rf_pa_channel runcam_board_id runcam_channel "
    "rf_cycle_period_ms rf_pa_warmup_ms rf_pa_tail_ms rf_pa_max_on_ms "
    "rf_pa_min_off_ms runcam_autostop_s rf_flags"))


def _rf_chain_defaults() -> dict:
    """Mirror of fmc_radio_config_defaults()'s RF-chain block, so a patch that
    omits the RF chain still produces a valid transaction instead of zeroes the
    firmware would reject."""
    return {
        "pa_board_id": 1, "pa_channel": 1,
        "runcam_board_id": 1, "runcam_channel": 0,
        "cycle_period_ms": 4000, "warmup_ms": 150, "tail_ms": 50,
        "max_on_ms": 1300, "min_off_ms": 2700,
        "duty_cycle": True,
        "runcam_autostop_s": 1800, "runcam_autostop": True,
        "boot_sound": False, "rec_on_power": True,
    }


def _peripheral_pair(chain: dict, board_key: str, channel_key: str) -> tuple:
    """One (board_id, channel) binding, or the unused sentinel pair."""
    board = chain.get(board_key)
    channel = chain.get(channel_key)
    if board is None or channel is None:
        return (FMC_RADIO_PERIPHERAL_UNUSED, FMC_RADIO_PERIPHERAL_UNUSED)
    board = int(board)
    channel = int(channel)
    if board == FMC_RADIO_PERIPHERAL_UNUSED and channel == FMC_RADIO_PERIPHERAL_UNUSED:
        return (FMC_RADIO_PERIPHERAL_UNUSED, FMC_RADIO_PERIPHERAL_UNUSED)
    if not 0 <= board <= 7 or not 0 <= channel <= 1:
        raise ValueError(f"{board_key}/{channel_key} must be EPB 0..7 channel 0..1")
    return (board, channel)


def _peripheral_binding(value: int):
    """One decoded board_id/channel, or None where the peripheral is unfitted."""
    return None if int(value) == FMC_RADIO_PERIPHERAL_UNUSED else int(value)


def pack_fmc_radio_config(config: dict, *, op: int = FMC_RADIO_CFG_SET_SAVE,
                          transaction_id: int = 0,
                          status: int = FMC_RADIO_CFG_REQUEST,
                          generation: int = 0, flags: int = 0,
                          validation_error: int = 0) -> bytes:
    """Encode the complete FMC vehicle-radio configuration bulk payload."""
    lora = config["lora"]
    if "pressure_channels" in config:
        raw_pressure = config["pressure_channels"]
    else:
        raw_pressure = (
            {"board_id": 0, "channel": 1},
            {"board_id": 2, "channel": 0},
            {"board_id": 4, "channel": 1},
        )
    if not isinstance(raw_pressure, (list, tuple)) or len(raw_pressure) > FMC_RADIO_PRESSURE_SLOTS:
        raise ValueError("at most three pressure channel selectors are allowed")
    pressure = []
    seen_pressure = set()
    for item in raw_pressure:
        if not isinstance(item, dict):
            raise ValueError("each pressure selector must be an object")
        try:
            board_id = int(item["board_id"])
            channel = int(item["channel"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("each pressure selector requires integer board_id and channel") from exc
        if not 0 <= board_id <= 7 or not 0 <= channel <= 1:
            raise ValueError("pressure board_id must be 0..7 and channel must be 0..1")
        selector = (board_id, channel)
        if selector in seen_pressure:
            raise ValueError("pressure channel selectors must be distinct")
        seen_pressure.add(selector)
        pressure.append({"board_id": board_id, "channel": channel})
    pressure.extend(
        {"board_id": FMC_RADIO_PRESSURE_UNUSED,
         "channel": FMC_RADIO_PRESSURE_UNUSED}
        for _ in range(FMC_RADIO_PRESSURE_SLOTS - len(pressure)))
    callsign = str(config.get("callsign") or "XXXXXX").strip().upper()
    if callsign == "NONE" or not callsign:
        callsign = "XXXXXX"
    encoded_callsign = callsign.encode("ascii")
    if len(encoded_callsign) > 15 or any(ch in b",*\r\n" for ch in encoded_callsign):
        raise ValueError("callsign must be 1..15 ASCII characters without delimiters")
    coding_rate = int(str(lora.get("coding_rate", "4/5")).split("/")[-1]) - 4

    chain = {**_rf_chain_defaults(), **dict(config.get("rf_chain") or {})}
    pa_board, pa_channel = _peripheral_pair(chain, "pa_board_id", "pa_channel")
    cam_board, cam_channel = _peripheral_pair(chain, "runcam_board_id",
                                              "runcam_channel")
    if (pa_board != FMC_RADIO_PERIPHERAL_UNUSED and
            (pa_board, pa_channel) == (cam_board, cam_channel)):
        raise ValueError(
            "the RF amplifier and the RunCam cannot share one load switch")
    cycle_period_ms = int(chain["cycle_period_ms"])
    warmup_ms = int(chain["warmup_ms"])
    tail_ms = int(chain["tail_ms"])
    max_on_ms = int(chain["max_on_ms"])
    min_off_ms = int(chain["min_off_ms"])
    autostop_s = int(chain["runcam_autostop_s"])
    for name, value, low, high in (
        ("rf_chain.cycle_period_ms", cycle_period_ms,
         FMC_RF_CYCLE_PERIOD_MIN_MS, FMC_RF_CYCLE_PERIOD_MAX_MS),
        ("rf_chain.warmup_ms", warmup_ms, 0, FMC_RF_WARMUP_MAX_MS),
        ("rf_chain.tail_ms", tail_ms, 0, FMC_RF_TAIL_MAX_MS),
        ("rf_chain.max_on_ms", max_on_ms, 1, FMC_RF_ON_MAX_MS),
        ("rf_chain.min_off_ms", min_off_ms, 0, FMC_RF_CYCLE_PERIOD_MAX_MS),
        ("rf_chain.runcam_autostop_s", autostop_s, 0,
         FMC_RF_RUNCAM_AUTOSTOP_MAX_S),
    ):
        if not low <= value <= high:
            raise ValueError(f"{name} must be {low}..{high}")
    # The same two feasibility rules the firmware enforces: a window has to
    # leave room to actually transmit, and a period has to contain the window
    # plus its mandated cool-down. Checking them here turns a rejected
    # transaction into an immediate, explainable error.
    if max_on_ms <= warmup_ms + tail_ms:
        raise ValueError("rf_chain.max_on_ms must exceed warmup_ms + tail_ms")
    if max_on_ms + min_off_ms > cycle_period_ms:
        raise ValueError(
            "rf_chain.max_on_ms + min_off_ms must fit inside cycle_period_ms")
    rf_flags = 0
    if chain.get("duty_cycle", True):
        rf_flags |= FMC_RF_FLAG_DUTY_CYCLE
    if chain.get("runcam_autostop", True):
        rf_flags |= FMC_RF_FLAG_RUNCAM_AUTOSTOP
    # A boot tone is an unattended noise on the pad, so it is opt-in; tying the
    # recording to the camera rail is what an operator expects, so that is
    # opt-out.
    if chain.get("boot_sound", False):
        rf_flags |= FMC_RF_FLAG_BOOT_SOUND
    if chain.get("rec_on_power", True):
        rf_flags |= FMC_RF_FLAG_REC_ON_POWER

    return struct.pack(
        FMC_RADIO_CONFIG_FMT,
        int(op) & 0xFF, int(status) & 0xFF, FMC_RADIO_CONFIG_VERSION,
        0, int(transaction_id) & 0xFFFFFFFF,
        int(generation) & 0xFFFFFFFF, int(config.get("network_id", 0x5554)) & 0xFFFF,
        int(config.get("vehicle_node_id", config.get("node_id", 1))) & 0xFFFF,
        int(config["allocation_low_hz"]), int(config["allocation_high_hz"]),
        int(lora["frequency_hz"]), int(lora["bandwidth_hz"]),
        int(lora.get("power_dbm", -10)), int(lora.get("spreading_factor", 10)),
        coding_rate, int(lora.get("preamble_symbols", 12)),
        *(int(item["board_id"]) for item in pressure),
        *(int(item["channel"]) for item in pressure),
        0, int(flags) & 0xFF,
        encoded_callsign.ljust(16, b"\x00"), int(validation_error) & 0xFFFFFFFF,
        pa_board, pa_channel, cam_board, cam_channel,
        cycle_period_ms, warmup_ms, tail_ms, max_on_ms, min_off_ms,
        autostop_s, rf_flags,
    )


def pack_fmc_radio_config_get(transaction_id: int = 0) -> bytes:
    """Encode a GET of the FMC's vehicle-radio record.

    A GET changes nothing on the FMC: it reads back its own authoritative
    record, and the body of the request is never looked at. Only op, status,
    version and transaction_id have to be right, and the frame has to be exactly
    88 bytes (FMC-Interface-Contract.md section 4.3).

    This exists because routing a GET through pack_fmc_radio_config() forced the
    caller to supply a complete, valid candidate config just to ask what the
    current one is - and supplying anything less raised KeyError: 'lora'. That
    made the read direction unusable without already knowing the answer.
    """
    blank = bytes(struct.calcsize(FMC_RADIO_CONFIG_FMT) - 8)
    return struct.pack("<BBBBI", FMC_RADIO_CFG_GET, FMC_RADIO_CFG_REQUEST,
                       FMC_RADIO_CONFIG_VERSION, 0,
                       int(transaction_id) & 0xFFFFFFFF) + blank


def unpack_fmc_radio_config(data: bytes) -> dict:
    """Decode one complete FMC vehicle-radio configuration/read-back payload."""
    if len(data) != struct.calcsize(FMC_RADIO_CONFIG_FMT):
        raise ValueError(f"FMC radio config is {len(data)} bytes, expected 88")
    v = _FmcRadioConfigWire._make(struct.unpack(FMC_RADIO_CONFIG_FMT, data))
    # These two bytes carried the profile selector and the auto-scan flag in
    # version 2. A sender that still populates them is describing a two-profile
    # vehicle, so every field around them means something different from what
    # this decoder would report. Refuse it instead of reinterpreting it.
    if v.reserved0 or v.reserved1:
        raise ValueError("FMC radio config sets a reserved byte; sender predates "
                         "the single-profile record")
    callsign = v.callsign.split(b"\x00", 1)[0].decode("ascii", "replace") or "XXXXXX"
    boards = (v.pressure_board_0, v.pressure_board_1, v.pressure_board_2)
    channels = (v.pressure_channel_0, v.pressure_channel_1, v.pressure_channel_2)
    pressure = []
    unused_seen = False
    seen_pressure = set()
    for board_id, channel in zip(boards, channels):
        board_id = int(board_id)
        channel = int(channel)
        board_unused = board_id == FMC_RADIO_PRESSURE_UNUSED
        channel_unused = channel == FMC_RADIO_PRESSURE_UNUSED
        if board_unused or channel_unused:
            if not (board_unused and channel_unused):
                raise ValueError("FMC pressure selector has a partial unused sentinel")
            unused_seen = True
            continue
        if unused_seen:
            raise ValueError("FMC pressure selectors must precede unused slots")
        if not 0 <= board_id <= 7 or not 0 <= channel <= 1:
            raise ValueError("FMC pressure selector is outside board/channel range")
        selector = (board_id, channel)
        if selector in seen_pressure:
            raise ValueError("FMC pressure selectors are not distinct")
        seen_pressure.add(selector)
        pressure.append({"board_id": board_id, "channel": channel})
    config = {
        "callsign": callsign,
        "role": "VEHICLE_TX_ONLY",
        "network_id": int(v.network_id),
        "node_id": int(v.vehicle_node_id),
        "vehicle_node_id": int(v.vehicle_node_id),
        "allocation_low_hz": int(v.allocation_low_hz),
        "allocation_high_hz": int(v.allocation_high_hz),
        "lora": {
            "frequency_hz": int(v.lora_frequency_hz), "modulation": "LORA",
            "bandwidth_hz": int(v.lora_bandwidth_hz),
            "power_dbm": int(v.lora_power_dbm),
            "spreading_factor": int(v.lora_sf),
            "coding_rate": f"4/{int(v.lora_cr) + 4}",
            "preamble_symbols": int(v.lora_preamble_symbols),
        },
        "pressure_channels": pressure,
        "rf_chain": {
            "pa_board_id": _peripheral_binding(v.rf_pa_board_id),
            "pa_channel": _peripheral_binding(v.rf_pa_channel),
            "runcam_board_id": _peripheral_binding(v.runcam_board_id),
            "runcam_channel": _peripheral_binding(v.runcam_channel),
            "cycle_period_ms": int(v.rf_cycle_period_ms),
            "warmup_ms": int(v.rf_pa_warmup_ms),
            "tail_ms": int(v.rf_pa_tail_ms),
            "max_on_ms": int(v.rf_pa_max_on_ms),
            "min_off_ms": int(v.rf_pa_min_off_ms),
            "runcam_autostop_s": int(v.runcam_autostop_s),
            "duty_cycle": bool(v.rf_flags & FMC_RF_FLAG_DUTY_CYCLE),
            "runcam_autostop": bool(v.rf_flags & FMC_RF_FLAG_RUNCAM_AUTOSTOP),
            "boot_sound": bool(v.rf_flags & FMC_RF_FLAG_BOOT_SOUND),
            "rec_on_power": bool(v.rf_flags & FMC_RF_FLAG_REC_ON_POWER),
        },
    }
    return {
        "op": int(v.op), "status": int(v.status),
        "status_name": FMC_RADIO_CFG_STATUS_NAMES.get(int(v.status), "unknown"),
        "version": int(v.version), "transaction_id": int(v.transaction_id),
        "generation": int(v.generation), "flags": int(v.flags),
        "persisted": bool(v.flags & FMC_RADIO_CFG_FLAG_PERSISTED),
        "link_ready": bool(v.flags & FMC_RADIO_CFG_FLAG_LINK_READY),
        "readback_matches": bool(v.flags & FMC_RADIO_CFG_FLAG_READBACK_MATCH),
        "placeholder_id": bool(v.flags & FMC_RADIO_CFG_FLAG_PLACEHOLDER_ID),
        "validation_error": int(v.validation_error), "config": config,
    }


def _runcam_autostop_arg16(autostop_s) -> int:
    """Map an API autostop_s onto the wire arg16 for the camera rail.

    None/absent -> 0xFFFF, defer to the FMC's persisted runcam_autostop_s.
    0           -> no timer; the rail stays up until something drops it.
    1..max      -> auto-stop that many seconds after the EPB echoes the rail up.

    A request that overflows the field clamps to the maximum instead of wrapping
    round into 0xFFFF, which would silently turn "as long as possible" into
    "whatever is persisted".
    """
    if autostop_s is None:
        return RT_AUX_RUNCAM_AUTOSTOP_DEFAULT
    return max(0, min(FMC_RF_RUNCAM_AUTOSTOP_MAX_S, int(autostop_s)))


def _encode_aux_power(device: int, enable: bool, arg16: int = 0) -> bytes:
    # rt_fmc_aux_power_t: u8 device, u8 enable, u16 arg16, 4 reserved.
    # arg16 is read for RT_AUX_DEV_RUNCAM only, where it bounds the recording
    # this power-up starts: 0 = no timer (rail stays up), 1..65534 = auto-stop
    # that many seconds after the EPB echoes the rail up, 0xFFFF = use the FMC's
    # persisted runcam_autostop_s. RADIO and RF_PA must send 0.
    return struct.pack("<BBH4x", device & 0xFF, 1 if enable else 0,
                       int(arg16) & 0xFFFF)


def _encode_sound_cmd(op: int, arg: int = 0, arg32: int = 0) -> bytes:
    # rt_fmc_sound_cmd_t: u8 op, u8 arg, u16 reserved, u32 arg32
    return struct.pack("<BBHI", op & 0xFF, arg & 0xFF, 0, arg32 & 0xFFFFFFFF)


def _encode_sound_begin(name: str, total_len: int, sample_rate: int, fmt: int) -> bytes:
    # rt_fmc_sound_begin_t (bulk): u32 total_len, u32 sample_rate, u16 format,
    # u16 reserved, char name[24]
    nb = name.encode("ascii", "replace")[:24]
    return struct.pack("<IIHH24s", total_len & 0xFFFFFFFF, sample_rate & 0xFFFFFFFF,
                       fmt & 0xFFFF, 0, nb)


def _encode_chg_en(enable: bool, i_setting: int = 0xFF, v_setting: int = 0xFF) -> bytes:
    # rt_pmb_chg_en_t: u8 enable, u8 i_setting, u8 v_setting + 5 reserved.
    # i_setting / v_setting = 0xFF means "leave the configured limit unchanged".
    return struct.pack("<BBB5x", 1 if enable else 0, i_setting & 0xFF, v_setting & 0xFF)


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

    # Actuator + IMC commands are addressed to the TARGET board kind (EPB), not
    # the sender (GS). The EPB firmware only applies its per-board_id filter when
    # the frame's kind == RT_BOARD_EPB (see epb main.c); with kind=GS the filter
    # is skipped and EVERY EPB executes the command regardless of board_id.
    if op == "pwm_set":
        pulse_us  = int(cmd.get("pulse_us", 0))
        period_us = int(cmd.get("period_us", 20000))
        cid  = can_id_pack(RT_MSG_PWM_SET, RT_BOARD_EPB, board_id, channel, 0, seq)
        return cid, _encode_pwm_set(pulse_to_q15(pulse_us, period_us), period_us)
    if op == "load_sw_set":
        enable  = bool(cmd.get("enable", False))
        hold_ms = int(cmd.get("hold_ms", 0))
        cid = can_id_pack(RT_MSG_LOAD_SW_SET, RT_BOARD_EPB, board_id, channel, 0, seq)
        return cid, _encode_load_sw_set(enable, hold_ms)
    if op == "failsafe":
        cid = can_id_pack(RT_MSG_ACTUATOR_FAILSAFE, RT_BOARD_EPB, board_id, 0, 0, seq)
        return cid, _pad8()
    if op == "imc_arm":
        cid = can_id_pack(RT_MSG_IGN_ARM, RT_BOARD_EPB, board_id, 0, 0, seq)
        return cid, _encode_imc_cmd(int(cmd.get("pulse_ms", 0)))
    if op == "imc_disarm":
        cid = can_id_pack(RT_MSG_IGN_DISARM, RT_BOARD_EPB, board_id, 0, 0, seq)
        return cid, _encode_imc_cmd(int(cmd.get("pulse_ms", 0)))
    if op == "discover":
        # Broadcast: DISCOVERY_REQ is sent from the GS to all boards.
        cid = can_id_pack(RT_MSG_DISCOVERY_REQ, RT_BOARD_GS, 0, 0, 0, seq)
        return cid, _pad8()
    if op == "actuator_query":
        cid = can_id_pack(RT_MSG_ACTUATOR_QUERY, RT_BOARD_EPB, board_id, channel, 0, seq)
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
    if op in ("rab_arm", "rab_disarm"):
        # Recovery Arming Board arm/disarm. Addressed to the RAB by board_id
        # (0 = A, 1 = B); the FMC pulses GPIO_ARM / GPIO_DISARM for pulse_ms.
        pulse_ms = int(cmd.get("pulse_ms", 100))
        rab_msg = RT_MSG_RECOVERY_ARM if op == "rab_arm" else RT_MSG_RAB_DISARM
        cid = can_id_pack(rab_msg, RT_BOARD_RAB, board_id, 0, 0, seq)
        return cid, _encode_rab_cmd(pulse_ms)
    if op == "aux_power":
        # FMC aux rail. Only "radio" is an FMC pin; "runcam" and "rf_pa" are EPB
        # load switches the FMC is the single writer for. "rfd" is accepted as a
        # deprecated alias for the vehicle modem so an older caller keeps working
        # rather than silently addressing device 0 by luck.
        dev = str(cmd.get("device", "radio")).lower()
        device = {
            "radio": RT_AUX_DEV_RADIO, "rfd": RT_AUX_DEV_RADIO,
            "modem": RT_AUX_DEV_RADIO,
            "runcam": RT_AUX_DEV_RUNCAM, "cam": RT_AUX_DEV_RUNCAM,
            "rf_pa": RT_AUX_DEV_RF_PA, "pa": RT_AUX_DEV_RF_PA,
            "amp": RT_AUX_DEV_RF_PA,
        }.get(dev)
        if device is None:
            raise ValueError(f"aux_power device must be radio/runcam/rf_pa, got {dev!r}")
        enable = bool(cmd.get("enable", False))
        # The camera rail IS the record control, so the auto-stop rides on this
        # command. An omitted autostop_s defers to the FMC's persisted default
        # rather than meaning "no timer" - 0 is a deliberate operator choice.
        arg16 = 0
        if device == RT_AUX_DEV_RUNCAM:
            arg16 = _runcam_autostop_arg16(cmd.get("autostop_s"))
        cid = can_id_pack(RT_MSG_FMC_AUX_POWER, RT_BOARD_FMC, board_id, 0, 0, seq)
        return cid, _encode_aux_power(device, enable, arg16)
    if op == "runcam_record":
        # Kept as a compatibility alias for callers that still say "record".
        # There is no record command in this system: the FMC has no data link to
        # the camera, and raising the 8V4 rail is what starts a recording. This
        # used to address device 3, which the firmware discards, so the timer
        # never armed. It now drives the real rail, identically to
        # aux_power(device="runcam").
        enable = bool(cmd.get("enable", False))
        arg16 = _runcam_autostop_arg16(cmd.get("autostop_s"))
        cid = can_id_pack(RT_MSG_FMC_AUX_POWER, RT_BOARD_FMC, board_id, 0, 0, seq)
        return cid, _encode_aux_power(RT_AUX_DEV_RUNCAM, enable, arg16)
    if op == "radio_config":
        # The complete FMC vehicle-radio profile, as one 88-byte bulk record.
        # Wired link only — the firmware never accepts this over RF.
        # A GET carries no candidate config, so it must not demand one.
        if str(cmd.get("action", "set")).lower() == "get":
            payload = pack_fmc_radio_config_get(
                transaction_id=int(cmd.get("transaction_id", 0)))
        else:
            cfg = cmd.get("cfg")
            if not isinstance(cfg, dict):
                raise ValueError("radio_config set requires a cfg object")
            payload = pack_fmc_radio_config(
                cfg, op=FMC_RADIO_CFG_SET_SAVE,
                transaction_id=int(cmd.get("transaction_id", 0)))
        cid = can_id_pack(RT_MSG_FMC_RADIO_CONFIG, RT_BOARD_FMC, board_id, 0, 0, seq)
        return cid, payload
    if op == "sound":
        # Soundboard control (buzzer replacement). action = play/stop/volume/tone/
        # list/clear. Clip upload (BEGIN/DATA bulk streaming) is not handled here.
        sub = str(cmd.get("action", "")).lower()
        snd_op = {"stop": SND_OP_STOP, "play": SND_OP_PLAY, "clear": SND_OP_CLEAR,
                  "volume": SND_OP_VOLUME, "list": SND_OP_LIST,
                  "tone": SND_OP_TONE}.get(sub)
        if snd_op is None:
            return None
        arg, arg32 = 0, 0
        if snd_op == SND_OP_PLAY:
            arg = int(cmd.get("idx", 0))
        elif snd_op == SND_OP_VOLUME:
            arg = int(cmd.get("volume", 255))
        elif snd_op == SND_OP_TONE:
            freq = int(cmd.get("freq_hz", 0)) & 0xFFFF
            ms = int(cmd.get("ms", 0)) & 0xFFFF
            arg32 = freq | (ms << 16)
        cid = can_id_pack(RT_MSG_FMC_SOUND_CMD, RT_BOARD_FMC, board_id, 0, 0, seq)
        return cid, _encode_sound_cmd(snd_op, arg, arg32)
    if op == "pmb_charger":
        # PMB battery charging (default-OFF). enable allows/suspends charging;
        # i_setting/v_setting are LTC4162 DAC codes (0..31), 0xFF = leave the
        # persisted limit unchanged so a plain enable/suspend never clobbers it.
        enable = bool(cmd.get("enable", False))
        i_set = int(cmd.get("i_setting", 0xFF)) & 0xFF
        v_set = int(cmd.get("v_setting", 0xFF)) & 0xFF
        cid = can_id_pack(RT_MSG_PMB_CHG_EN, RT_BOARD_PMB, board_id, 0, 0, seq)
        return cid, _encode_chg_en(enable, i_set, v_set)
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
        try:
            return op_to_frame(str(pkt["op"]), pkt, seq)
        except ValueError:
            return None
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


def _close_quietly(ser) -> None:
    """Close a serial handle we are done with; a port that has already gone away
    often raises on close, and there is nothing useful to do about it."""
    if ser is None:
        return
    try:
        ser.close()
    except Exception:
        pass


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
                 imc_board_id: int | None = None, data_dir: str = "data",
                 ops_url: str = "") -> None:
        self._port       = port
        self._baud       = baud
        self._node_id    = node_id
        # Base URL of the novaOps backend, used to download staged soundboard
        # clips. Overrides the URL the backend put in the command, which may name
        # an address (e.g. localhost) that is not reachable from this host.
        self._ops_url    = ops_url or ""
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
        # Protocol update: RAB (recovery arming) keyed "RAB:0"(A)/"RAB:1"(B),
        # FMC aux (RunCam + PPS), FMC RF rate/power mode, and soundboard.
        self._fas_rab: dict[str, dict] = {}
        self._fas_aux: dict = {}
        self._fas_radio_cfg: dict = {}
        self._fas_sound: dict = {"status": {}, "clips": {}}  # clips: idx -> clip dict
        # Soundboard clip upload runs on its own thread; only one at a time.
        self._upload_lock = threading.Lock()
        self._uploading = False
        # Flight FSM (fas_state + flight_phase) derived from FMC baro + IMC arm.
        self._fsm = FlightFsm()
        self._fsm_last_t = time.monotonic()
        self._console_active = False

        # Serial — port/baud are mutable so the console can reconfigure them, and
        # _serial is None whenever no port is open. The bridge is fully functional
        # without one: it still serves MQTT/console so novaOps can set the port.
        self._serial_lock = threading.Lock()
        self._serial: serial.Serial | None = None
        self._serial_error: str | None = None
        self._retry_at   = 0.0    # monotonic deadline for the next reopen attempt
        self._parser  = FrameParser(self._on_frame)

        # MQTT
        # The client ID must be unique per broker connection. prod and dev each
        # run a bridge against the same broker, and two clients sharing an ID
        # make the broker evict whichever connected first - an endless
        # connect/disconnect loop that shows up as "MQTT disconnected rc=7" and
        # silently drops commands and telemetry in both environments.
        # The published `source` deliberately stays node_id ("FAS"), because the
        # backend routes FAS sensors on that exact string (_SOURCE_ALIASES);
        # only the client ID is disambiguated, by the ops-URL port that already
        # distinguishes prod (8000) from dev (8001).
        _ops_port = None
        try:
            _ops_port = urllib.parse.urlparse(self._ops_url).port
        except (ValueError, AttributeError):
            _ops_port = None
        self._mqtt_client_id = (f"{node_id}-{_ops_port}" if _ops_port
                                else f"{node_id}-{os.getpid()}")
        self._client = mqtt.Client(client_id=self._mqtt_client_id,
                                   clean_session=True)
        self._client.on_connect    = self._on_connect
        self._client.on_message    = self._on_message
        self._client.on_disconnect = self._on_disconnect
        host, port_num = _parse_broker(broker)
        self._client.connect(host, port_num, keepalive=60)

        # Opened last so a failure is only reported, never fatal. The read loop
        # keeps retrying a configured-but-unavailable port.
        if port:
            self._open_serial(port, baud)

    # ── MQTT callbacks ───────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc: int) -> None:
        if rc == 0:
            self._log(1, f"[bridge] MQTT connected as '{self._mqtt_client_id}' "
                     f"(source={self._node_id})")
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
          disconnect          — close the serial port and stay idle
          status              — report the current serial link state
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
            port = str(cmd.get("port", "")).strip() or self._port
            baud = int(cmd.get("baud", self._baud))
            if not port:
                self._publish_console({
                    "type": "console_config", "ok": False, "port": "", "baud": baud,
                    "error": "no port given and none configured",
                })
                return
            ok, err = self._open_serial(port, baud)
            self._publish_console({
                "type": "console_config",
                "ok": ok, "port": port, "baud": baud,
                **({"error": err} if err else {}),
            })

        elif action == "disconnect":
            self._close_serial()
            self._publish_console({"type": "console_config", "ok": True,
                                   "port": "", "baud": self._baud})

        elif action == "status":
            self._publish_serial_state()

        elif action == "tx":
            frame = console_packet_to_frame(cmd, self._next_seq())
            if frame is None:
                self._log(2, f"[bridge] console tx: cannot encode {cmd}")
                self._publish_console({"type": "console_tx",
                                       "ok": False, "error": "unencodable packet"})
                return
            can_id, data = frame
            sent = self._send_frame(can_id, data)
            hex_frame = encode_frame(can_id, data).hex()
            decoded   = can_id_unpack(can_id)
            self._log(1, f"[bridge] console tx msg=0x{decoded['msg']:02x} "
                         f"board={decoded['board_id']} ch={decoded['channel']}"
                         f"{'' if sent else ' (dropped: no serial port)'}")
            self._publish_console({
                "type":      "console_tx",
                "ok":        sent,
                **({} if sent else {"error": "no serial port connected"}),
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

    # ── Serial link lifecycle ────────────────────────────────────────────────

    def _serial_state(self) -> dict:
        """Current link state, as published on nova/console and in the flight
        payload so novaOps can show whether the bridge has a port."""
        with self._serial_lock:
            return {"connected": self._serial is not None,
                    "port": self._port, "baud": self._baud,
                    "error": self._serial_error}

    def _publish_serial_state(self) -> None:
        self._publish_console({"type": "console_serial", **self._serial_state()})

    def _open_serial(self, port: str, baud: int) -> tuple[bool, str | None]:
        """Open `port` and make it the active handle, closing any previous one.

        Never raises — a failure is recorded, reported to novaOps and retried by
        the read loop. Returns (ok, error_message)."""
        try:
            new_serial = serial.Serial(port, baud, timeout=1)
        except (serial.SerialException, ValueError, OSError) as e:
            with self._serial_lock:
                repeat = self._serial_error == str(e) and self._port == port
                self._port, self._baud = port, baud
                self._serial_error = str(e)
                self._retry_at = time.monotonic() + SERIAL_RETRY_S
            # Only shout the first time: the retry loop would otherwise spam.
            self._log(2 if repeat else 0, f"[bridge] serial open failed on {port}: {e}")
            if not repeat:
                self._publish_serial_state()
            return False, str(e)

        with self._serial_lock:
            old = self._serial
            self._serial = new_serial
            self._port, self._baud = port, baud
            self._serial_error = None
        _close_quietly(old)
        self._log(1, f"[bridge] serial connected -> {port} @ {baud} baud")
        self._publish_serial_state()
        return True, None

    def _drop_serial(self, ser, reason: str) -> None:
        """Retire a handle that failed mid-use. A no-op if it has already been
        replaced (e.g. by a concurrent reconfigure), so the live port survives."""
        with self._serial_lock:
            if self._serial is not ser:
                return
            self._serial = None
            self._serial_error = reason
            self._retry_at = time.monotonic() + SERIAL_RETRY_S
            port = self._port
        _close_quietly(ser)
        self._log(0, f"[bridge] serial port {port} lost: {reason}")
        self._publish_serial_state()

    def _close_serial(self) -> None:
        """Explicitly disconnect and stay idle until a new port is configured."""
        with self._serial_lock:
            ser = self._serial
            self._serial = None
            self._serial_error = None
            self._port = ""
        _close_quietly(ser)
        self._log(1, "[bridge] serial disconnected")
        self._publish_serial_state()

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
        # A soundboard clip upload streams BEGIN/DATA(bulk)/END to the FMC over
        # many frames, paced to what the flash can sustain — run it off-thread so
        # it never blocks the MQTT/command loop.
        if op == "sound_upload":
            self._cmd_sound_upload(cmd)
            return
        try:
            frame = op_to_frame(op, cmd, self._next_seq())
        except ValueError as e:
            # A rejected payload is not an unknown op, and saying so is the
            # difference between "typo" and "your radio config is invalid".
            self._log(0, f"[bridge] fas op={op!r} rejected: {e}")
            return
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

    # ── Soundboard clip upload (BEGIN / DATA bulk / END) ──────────────────────
    #
    # The clip bytes are already transcoded to the on-flash format by the backend.
    # They are NOT carried in the MQTT command (that capped clips at a few hundred
    # KB); the command carries a one-shot download URL instead, which we fetch to
    # a temp file and then stream to the FMC:
    #   download (verify length + crc32) -> ABORT (until idle) -> BEGIN (bulk) ->
    #   wait UL_READY (erase done) -> DATA (<=256-byte bulk frames, paced) ->
    #   END (crc32) -> verify clip_count.
    # A legacy `data_b64` command is still accepted for older backends.
    # Progress and the final outcome are published on the console topic as
    # sound_upload_progress / sound_upload_result so the backend can complete the
    # HTTP request that started the upload.
    # Mirrors the gs server's _upload_once; the pacing keeps the FMC's flash
    # programming from overrunning its USB/serial RX ring (see gs comments).
    UL_BULK_MAX   = 256      # data bytes per bulk DATA frame
    UL_BYTES_PER_S = 40000   # pacing: safe below the FMC flash-write knee
    UL_BURST      = 16       # frames per burst before pacing sleep
    UL_DOWNLOAD_TIMEOUT_S = 30.0   # per-read timeout on the clip download
    UL_DOWNLOAD_MAX_BYTES = 32 << 20  # refuse an absurd clip before writing it
    UL_PROGRESS_INTERVAL_S = 1.0   # how often to publish upload progress

    def _cmd_sound_upload(self, cmd: dict) -> None:
        name = str(cmd.get("name", "clip"))
        fmt = int(cmd.get("format", SND_FMT_PCM_S16))
        rate = int(cmd.get("sample_rate", 31250))
        upload_id = str(cmd.get("upload_id") or "")
        url = self._clip_url(cmd)
        raw_b64 = str(cmd.get("data_b64", "")) if "data_b64" in cmd else ""

        if not url and not raw_b64:
            self._ul_result(upload_id, name, False, "command",
                            "no clip url and no inline data")
            return

        with self._upload_lock:
            if self._uploading:
                self._ul_result(upload_id, name, False, "busy",
                                "another upload is in progress")
                return
            self._uploading = True
        threading.Thread(
            target=self._run_sound_upload,
            args=(upload_id, name, url, raw_b64, cmd.get("crc32"),
                  int(cmd.get("bytes", 0) or 0), rate, fmt),
            daemon=True).start()

    def _clip_url(self, cmd: dict) -> str:
        """Absolute URL to download the staged clip from, or "" if the command
        carries none. --ops-url (if given) overrides the host the backend
        guessed, which matters when the backend sees itself as localhost."""
        url = str(cmd.get("url") or "")
        path = str(cmd.get("path") or "")
        if self._ops_url:
            if not path and url:
                parts = urllib.parse.urlsplit(url)
                path = urllib.parse.urlunsplit(("", "", parts.path, parts.query, ""))
            return urllib.parse.urljoin(self._ops_url.rstrip("/") + "/",
                                        path.lstrip("/")) if path else ""
        if url.startswith(("http://", "https://")):
            return url
        if url or path:
            self._log(0, "[bridge] sound_upload: relative clip url and no "
                         "--ops-url configured — cannot download the clip")
        return ""

    def _run_sound_upload(self, upload_id, name, url, raw_b64, crc_hint,
                          size_hint, rate, fmt) -> None:
        tmp_path = None
        try:
            crc32 = (int(crc_hint) & 0xFFFFFFFF) if crc_hint is not None else None
            if url:
                tmp_path, total, crc_actual = self._download_clip(url)
                if tmp_path is None:
                    self._ul_result(upload_id, name, False, "download",
                                    f"download failed: {crc_actual}")
                    return
                if size_hint and total != size_hint:
                    self._ul_result(upload_id, name, False, "download",
                                    f"clip length {total} != expected {size_hint}")
                    return
                if crc32 is not None and crc_actual != crc32:
                    self._ul_result(upload_id, name, False, "download",
                                    f"clip crc32 {crc_actual:08x} != "
                                    f"expected {crc32:08x}")
                    return
                crc32 = crc_actual
                chunks = self._file_chunks(tmp_path)
            else:
                try:
                    data = base64.b64decode(raw_b64, validate=True)
                except (binascii.Error, ValueError):
                    self._ul_result(upload_id, name, False, "decode",
                                    "invalid base64 data")
                    return
                total = len(data)
                if crc32 is None:
                    crc32 = zlib.crc32(data) & 0xFFFFFFFF
                chunks = self._bytes_chunks(data)

            if not total:
                self._ul_result(upload_id, name, False, "decode", "empty clip")
                return

            ok, stage, err, count = self._do_sound_upload(
                upload_id, name, chunks, total, crc32, rate, fmt)
            self._log(1, f"[bridge] sound_upload {name!r} ({total} B) "
                         f"{'OK' if ok else 'FAILED at ' + stage}")
            self._ul_result(upload_id, name, ok, stage, err,
                            total=total, clip_count=count)
        except Exception as e:                       # noqa: BLE001
            self._log(0, f"[bridge] sound_upload error: {e}")
            self._ul_result(upload_id, name, False, "error", str(e))
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            with self._upload_lock:
                self._uploading = False

    def _download_clip(self, url: str):
        """Fetch the staged clip to a temp file. Returns (path, bytes, crc32) on
        success, or (None, 0, error_message) on failure."""
        fd, tmp_path = tempfile.mkstemp(prefix="fas_clip_", suffix=".bin")
        crc = 0
        total = 0
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fas_bridge"})
            with urllib.request.urlopen(req, timeout=self.UL_DOWNLOAD_TIMEOUT_S) as resp, \
                    os.fdopen(fd, "wb") as out:
                fd = None  # now owned by `out`
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self.UL_DOWNLOAD_MAX_BYTES:
                        raise ValueError(
                            f"clip exceeds {self.UL_DOWNLOAD_MAX_BYTES} bytes")
                    crc = zlib.crc32(chunk, crc)
                    out.write(chunk)
        except (urllib.error.URLError, OSError, ValueError) as e:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return None, 0, str(e)
        self._log(1, f"[bridge] sound_upload: downloaded {total} B from {url}")
        return tmp_path, total, crc & 0xFFFFFFFF

    def _file_chunks(self, path: str):
        def gen():
            with open(path, "rb") as f:
                while True:
                    block = f.read(self.UL_BULK_MAX)
                    if not block:
                        return
                    yield block
        return gen()

    def _bytes_chunks(self, data: bytes):
        def gen():
            for off in range(0, len(data), self.UL_BULK_MAX):
                yield data[off:off + self.UL_BULK_MAX]
        return gen()

    def _ul_result(self, upload_id, name, ok, stage, error=None,
                   total=None, clip_count=None) -> None:
        """Publish the outcome of an upload so the backend's HTTP request can
        finish. Failures are logged too, since a bridge run may have no backend."""
        if not ok:
            self._log(0, f"[bridge] sound_upload {name!r} failed at {stage}: {error}")
        self._publish_console({
            "type": "sound_upload_result", "upload_id": upload_id,
            "name": name, "ok": bool(ok), "stage": stage, "error": error,
            "bytes": total, "clip_count": clip_count, "source": self._node_id,
        })

    def _ul_progress(self, upload_id, name, sent, total) -> None:
        self._publish_console({
            "type": "sound_upload_progress", "upload_id": upload_id,
            "name": name, "sent": sent, "total": total,
            "pct": round(100.0 * sent / total, 1) if total else 0.0,
            "source": self._node_id,
        })

    def _snd_status(self, key, default=None):
        with self._lock:
            st = self._fas_sound.get("status")
            return st.get(key, default) if isinstance(st, dict) else default

    def _send_bulk_frame(self, can_id: int, data: bytes) -> None:
        """Send a large-payload frame (up to 256 data bytes). Same framing as a
        classic frame — only the length is larger (see egse_uart.c)."""
        self._send_frame(can_id, data[:self.UL_BULK_MAX])

    def _do_sound_upload(self, upload_id, name, chunks, total, crc32, rate, fmt):
        """Stream `chunks` (<=UL_BULK_MAX blocks totalling `total` bytes) to the
        FMC. Returns (ok, stage, error, clip_count)."""
        with self._serial_lock:
            connected = self._serial is not None
        if not connected:
            return False, "serial", "no serial port connected", None

        count0 = self._snd_status("clip_count", 0) or 0

        # Reset any half-finished prior upload; retry ABORT until the FMC reports
        # it actually went idle (a single ABORT can be lost).
        for _ in range(20):
            self._send_frame(
                can_id_pack(RT_MSG_FMC_SOUND_CMD, RT_BOARD_FMC, 0, 0, 0, self._next_seq()),
                _encode_sound_cmd(SND_OP_UL_ABORT))
            time.sleep(0.12)
            if not self._snd_status("ul_active"):
                break

        # BEGIN (bulk) — the FMC erases the clip region, then reports UL_READY.
        self._send_bulk_frame(
            can_id_pack(RT_MSG_FMC_SOUND_BEGIN, RT_BOARD_FMC, 0, 0, 0, self._next_seq()),
            _encode_sound_begin(name, total, rate, fmt))
        t0 = time.monotonic()
        while not self._snd_status("ul_ready"):
            if time.monotonic() - t0 > 25.0:
                return False, "begin", "timed out waiting for flash erase", None
            time.sleep(0.03)

        # DATA — stream the clip in <=256-byte bulk frames, paced so the FMC's
        # per-frame flash programming doesn't overrun its RX ring.
        sent = 0
        t_pace = time.monotonic()
        t_report = t_pace
        for i, block in enumerate(chunks):
            self._send_bulk_frame(
                can_id_pack(RT_MSG_FMC_SOUND_DATA, RT_BOARD_FMC, 0, 0, 0, self._next_seq()),
                block)
            sent += len(block)
            if (i % self.UL_BURST) == (self.UL_BURST - 1):
                behind = (t_pace + sent / self.UL_BYTES_PER_S) - time.monotonic()
                if behind > 0:
                    time.sleep(behind)
                now = time.monotonic()
                if now - t_report >= self.UL_PROGRESS_INTERVAL_S:
                    t_report = now
                    self._ul_progress(upload_id, name, sent, total)

        # END — the FMC verifies CRC + length and commits. Let TX drain first so
        # END never overtakes the last DATA frames.
        time.sleep(0.15)
        self._send_frame(
            can_id_pack(RT_MSG_FMC_SOUND_CMD, RT_BOARD_FMC, 0, 0, 0, self._next_seq()),
            _encode_sound_cmd(SND_OP_UL_END, arg32=crc32))

        # Verify the commit landed: normally clip_count rises, but overwriting an
        # existing clip leaves the count unchanged — so also accept the clip
        # directory reporting this name at this length. LIST refreshes it.
        self._send_frame(
            can_id_pack(RT_MSG_FMC_SOUND_CMD, RT_BOARD_FMC, 0, 0, 0, self._next_seq()),
            _encode_sound_cmd(SND_OP_LIST))
        t0 = time.monotonic()
        while time.monotonic() - t0 < 6.0:
            count = self._snd_status("clip_count", 0) or 0
            if count > count0 or self._has_clip(name, total):
                return True, "done", None, count
            time.sleep(0.05)
        return (False, "verify",
                "the FMC did not report the new clip (CRC or length mismatch?)",
                self._snd_status("clip_count", 0))

    def _has_clip(self, name: str, length: int) -> bool:
        """True when the FMC's clip directory lists `name` at exactly `length`."""
        want = name.encode("ascii", "replace")[:24].decode("ascii", "replace")
        with self._lock:
            clips = list(self._fas_sound.get("clips", {}).values())
        return any(c.get("name") == want and c.get("length") == length for c in clips)

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
                     RT_MSG_PMB_TEMP, RT_MSG_PMB_CHARGER, RT_MSG_PMB_CHG_CFG):
            field = {
                RT_MSG_PMB_PWR:     "pwr",
                RT_MSG_PMB_VMON:    "vmon",
                RT_MSG_PMB_TEMP:    "temp",
                RT_MSG_PMB_CHARGER: "charger",
                RT_MSG_PMB_CHG_CFG: "chg_cfg",
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

        elif msg == RT_MSG_RAB_STATUS:
            # Store the RAB status AND register the RAB as an online board so it
            # appears in the fleet. RABs report over the FMC's link, keyed by the
            # polled ID (RAB:0 = A, RAB:1 = B).
            with self._lock:
                self._fas_rab[key] = {**d, "online": True, "last_seen": now}
                prev = self._fas_boards.get(key, {})
                self._fas_boards[key] = {
                    **prev, "kind": kind_name, "board_id": board_id,
                    "online": True, "last_seen": now,
                    "uptime_ms": prev.get("uptime_ms", 0),
                    "num_channels": prev.get("num_channels", 0),
                    "num_sensors": prev.get("num_sensors", 0),
                    "fw_version": prev.get("fw_version", 0),
                }
            self._log(2, f"[bridge] RAB status {key} fc_armed={d.get('fc_armed')} "
                         f"mismatch={d.get('arm_mismatch')}")

        elif msg == RT_MSG_FMC_AUX_STATUS:
            with self._lock:
                self._fas_aux = {**d, "last_seen": now}
            self._log(2, f"[bridge] FMC aux runcam={d.get('runcam_powered')} "
                         f"autostop={d.get('runcam_autostop')} "
                         f"rec_s={d.get('runcam_record_s')} "
                         f"pa={d.get('rf_pa_on')} pps={d.get('pps_present')}")

        elif msg == RT_MSG_FMC_RADIO_CONFIG:
            with self._lock:
                self._fas_radio_cfg = {**d, "last_seen": now}
            if "config_decode_error" in d:
                self._log(0, "[bridge] FMC radio config undecodable: "
                             f"{d['config_decode_error']}")
            else:
                self._log(2, f"[bridge] FMC radio config {d.get('status_name')} "
                             f"txn={d.get('transaction_id')} "
                             f"persisted={d.get('persisted')}")

        elif msg == RT_MSG_FMC_SOUND_STATUS:
            with self._lock:
                self._fas_sound["status"] = d
                # A fresh count of 0 means the board was cleared — drop the dir.
                if d.get("clip_count") == 0:
                    self._fas_sound["clips"] = {}
            self._log(2, f"[bridge] sound status clips={d.get('clip_count')} "
                         f"playing={d.get('playing_idx')}")

        elif msg == RT_MSG_FMC_SOUND_CLIP:
            if "idx" in d:
                with self._lock:
                    self._fas_sound["clips"][d["idx"]] = d
                self._log(2, f"[bridge] sound clip #{d.get('idx')} {d.get('name')!r}")

    # ── FAS frame send helpers ────────────────────────────────────────────────

    def _next_seq(self) -> int:
        with self._lock:
            s = self._seq
            self._seq = (self._seq + 1) & 0xFF
        return s

    def _send_frame(self, can_id: int, data: bytes) -> bool:
        """Write one frame. Returns False (without raising) when there is no port
        or the write failed — a failed write retires the handle so the read loop
        reconnects."""
        frame = encode_frame(can_id, data)
        with self._serial_lock:
            ser = self._serial
        if ser is None:
            self._log(2, "[bridge] no serial port — frame dropped")
            return False
        try:
            ser.write(frame)
            return True
        except (serial.SerialException, OSError) as e:
            self._drop_serial(ser, f"write failed: {e}")
            return False

    def _send_discovery_req(self) -> None:
        cid = can_id_pack(RT_MSG_DISCOVERY_REQ, RT_BOARD_GS, 0, 0, 0, self._next_seq())
        self._send_frame(cid, _pad8())

    def _send_pwm_set(self, board_id: int, channel: int,
                      pulse_us: int, period_us: int = 20000) -> None:
        cid     = can_id_pack(RT_MSG_PWM_SET, RT_BOARD_EPB, board_id, channel, 0, self._next_seq())
        duty    = pulse_to_q15(pulse_us, period_us)
        payload = _encode_pwm_set(duty, period_us)
        self._send_frame(cid, payload)

    def _send_load_sw_set(self, board_id: int, channel: int,
                          enable: bool, hold_ms: int = 0) -> None:
        cid     = can_id_pack(RT_MSG_LOAD_SW_SET, RT_BOARD_EPB, board_id, channel, 0, self._next_seq())
        payload = _encode_load_sw_set(enable, hold_ms)
        self._send_frame(cid, payload)

    def _send_imc_arm(self, board_id: int, pulse_ms: int = 0) -> None:
        cid     = can_id_pack(RT_MSG_IGN_ARM, RT_BOARD_EPB, board_id, 0, 0, self._next_seq())
        self._send_frame(cid, _encode_imc_cmd(pulse_ms))

    def _send_imc_disarm(self, board_id: int, pulse_ms: int = 0) -> None:
        cid     = can_id_pack(RT_MSG_IGN_DISARM, RT_BOARD_EPB, board_id, 0, 0, self._next_seq())
        self._send_frame(cid, _encode_imc_cmd(pulse_ms))

    # ── Background loops ─────────────────────────────────────────────────────

    def _serial_read_loop(self) -> None:
        self._log(1, f"[bridge] serial reader started on {self._port or '(no port)'}")
        while True:
            with self._serial_lock:
                ser = self._serial
                port, baud, retry_at = self._port, self._baud, self._retry_at

            if ser is None:
                # Either nothing is configured yet — idle until novaOps sends a
                # console "configure" — or the configured port is due a retry.
                if port and time.monotonic() >= retry_at:
                    self._open_serial(port, baud)
                else:
                    time.sleep(0.2)
                continue

            try:
                chunk = ser.read(256)
            except (serial.SerialException, OSError) as e:
                # A reconfigure may have closed this handle out from under us, in
                # which case _drop_serial leaves the new one alone; otherwise the
                # port really went away and gets retried on the next passes.
                self._drop_serial(ser, str(e))
                continue

            if chunk:
                # Deliberately outside the read's except: pyserial's
                # SerialException subclasses OSError, so catching OSError around
                # the decode too would let an unrelated fault downstream of
                # feed() -- a malformed payload, or the MQTT publish _on_frame
                # makes while the console streams -- retire a healthy port and
                # show up in the UI as a link flap.
                try:
                    self._parser.feed(chunk)
                except Exception:
                    detail = traceback.format_exc().strip().splitlines()[-1]
                    self._log(0, f"[bridge] frame handling failed ({detail}) — "
                                 "port kept, frame dropped")

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
                # Protocol-update blocks: RAB (recovery arming), FMC aux, RF mode,
                # soundboard. Drop the internal last_seen bookkeeping key.
                fas_rab_snap = {
                    k: {kk: vv for kk, vv in v.items() if kk != "last_seen"}
                    for k, v in self._fas_rab.items()
                }
                fas_aux_snap = {kk: vv for kk, vv in self._fas_aux.items() if kk != "last_seen"}
                fas_radio_cfg_snap = {kk: vv for kk, vv in self._fas_radio_cfg.items()
                                      if kk != "last_seen"}
                fas_sound_snap = {
                    "status": dict(self._fas_sound.get("status", {})),
                    "clips": [self._fas_sound["clips"][i]
                              for i in sorted(self._fas_sound["clips"])],
                }
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
                    "fas_rab":          fas_rab_snap,
                    "fas_aux":          fas_aux_snap,
                    "fas_radio_cfg":    fas_radio_cfg_snap,
                    "fas_sound":        fas_sound_snap,
                    # Serial link state, so the frontend can show whether the
                    # bridge actually has a port and which one.
                    "fas_link":         self._serial_state(),
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
        where = f"{self._port} @ {self._baud} baud" if self._port else \
            "no serial port (set one from novaOps)"
        self._log(1, f"[bridge] FAS bridge starting: {where}")

        for target in (self._serial_read_loop, self._discovery_loop, self._publish_loop):
            threading.Thread(target=target, daemon=True).start()

        self._client.loop_forever()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="FAS RS-422 to MQTT bridge — runs in place of novaGround's FAS integration",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--port",       default=os.getenv("NOVA_FAS_PORT", ""),
                   help="Serial port connected to the FAS FMC bridge (e.g. "
                        "/dev/ttyUSB0). Optional: without it the bridge starts "
                        "idle and waits for novaOps to set a port. Defaults to "
                        "$NOVA_FAS_PORT")
    p.add_argument("--baud",       type=int, default=115200)
    p.add_argument("--broker",     default="localhost:1883")
    p.add_argument("--node-id",    default="FAS",
                   help="Telemetry 'source' string published with every FAS "
                        "message. The backend routes FAS sensors on this exact "
                        "value, so leave it as 'FAS'. The MQTT client ID is "
                        "derived from it plus the ops-URL port, so prod and dev "
                        "never collide on the broker.")
    p.add_argument("--publish-ms", type=int, default=50,
                   help="Telemetry publish interval ms")
    p.add_argument("--verbosity",  type=int, default=1, choices=[0, 1, 2])
    p.add_argument("--imc-board-id", type=int, default=0,
                   help="If set, only update IMC arm state from this board_id; "
                        "IMC_STATUS frames from other boards are ignored "
                        "(default: accept any board)")
    p.add_argument("--data-dir",   default="data",
                   help="Directory for recorded data-saving CSV files")
    p.add_argument("--ops-url",    default=os.getenv("NOVA_OPS_URL", ""),
                   help="novaOps backend base URL (e.g. http://10.0.0.5:8000) "
                        "used to download staged soundboard clips. Defaults to "
                        "$NOVA_OPS_URL; when unset, the URL in the command is "
                        "used as-is")
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
        ops_url=args.ops_url,
    ).run()


if __name__ == "__main__":
    main()
