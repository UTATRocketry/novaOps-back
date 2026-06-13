#!/usr/bin/env python3
"""
nova_dummy.py  —  Software mock of a running novaGround instance.

Connects to the MQTT broker, subscribes to nova/command, handles every
command type that a real novaGround would (servo, relay, gpio, fas,
console, data_file) and publishes nova/telemetry/engine (sensors + GPIO) and
nova/telemetry/flight (FAS board status + IMC state) at a configurable rate.

Simulated hardware
  • 2 MCC DAQ hats (hat_id 0 and 1), 8 ADC channels each — slow sine waves
  • 2 FAS EPB boards (board_id 0 and 1) — always online, incrementing uptime
  • 3 GPIO output pins (17, 27, 22) — settable via command
  • 16 relay channels — settable via command
  • FAS IMC (board_id 0) — arm/disarm state follows commands
  • Console mode: when active, publishes a fake heartbeat frame to nova/console

Dependencies: pip install paho-mqtt

Usage:
    python nova_dummy.py [--broker host[:port]] [--node-id NAME]
                         [--publish-ms MS] [--verbosity 0|1|2]
"""
from __future__ import annotations

import argparse
import json
import math
import random
import threading
import time

import paho.mqtt.client as mqtt

# ── Topics ──────────────────────────────────────────────────────────────────
COMMAND_TOPIC   = "nova/command"
TELEMETRY_TOPIC = "nova/telemetry/engine"
FLIGHT_TOPIC    = "nova/telemetry/flight"
CONSOLE_TOPIC   = "nova/console"
EXPECTED_SOURCE = "novaOps"

# ── FAS constants ────────────────────────────────────────────────────────────
ADC_INT16_TO_V  = 256.0 * 1.2 / (1 << 23)
ADC_INT16_TO_MA = ADC_INT16_TO_V * 1000.0 / 100.0

# ── Fixed simulated topology ─────────────────────────────────────────────────
MOCK_HATS      = [{"hat_id": 0, "channels": 8}, {"hat_id": 1, "channels": 8}]
# EPB board_ids matching config (EPB_1=0, EPB_2=1, EPB_3=2); node string = "EPB_{board_id+1}"
MOCK_EPB_IDS   = [0, 1, 2]
MOCK_GPIO_PINS = [17, 27, 22]


# ─────────────────────────────────────────────────────────────────────────────
class NovaDummy:
    def __init__(self, broker: str, node_id: str, publish_ms: int, verbosity: int) -> None:
        self._node_id     = node_id
        self._publish_ms  = publish_ms
        self._verbosity   = verbosity
        self._t0          = time.monotonic()
        self._lock        = threading.Lock()

        # Mutable state -------------------------------------------------------
        self._relay_states   = {i: False for i in range(16)}
        self._servo_states: dict[int, int] = {}           # channel → last pulse_us
        self._gpio_states    = {pin: 0 for pin in MOCK_GPIO_PINS}
        self._fas_imc        = {"board_id": 0, "armed": False,
                                "arm_line": False, "disarm_line": False}
        self._console_active = False

        # MQTT ----------------------------------------------------------------
        self._client = mqtt.Client(client_id=node_id, clean_session=True)
        self._client.on_connect    = self._on_connect
        self._client.on_message    = self._on_message
        self._client.on_disconnect = self._on_disconnect
        host, port = _parse_broker(broker)
        self._client.connect(host, port, keepalive=60)

    # ── MQTT callbacks ───────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc: int) -> None:
        if rc == 0:
            self._log(1, f"[dummy] connected as '{self._node_id}'")
            client.subscribe(COMMAND_TOPIC, qos=1)
        else:
            self._log(0, f"[dummy] MQTT connect failed rc={rc}")

    def _on_disconnect(self, client, userdata, rc: int) -> None:
        self._log(1, f"[dummy] disconnected rc={rc}, reconnecting…")

    def _on_message(self, client, userdata, msg) -> None:
        try:
            env = json.loads(msg.payload)
        except Exception:
            return
        if not isinstance(env, dict) or env.get("source") != EXPECTED_SOURCE:
            return
        cmd = env.get("command")
        if isinstance(cmd, dict):
            self._dispatch(cmd)

    # ── Command dispatch ─────────────────────────────────────────────────────

    def _dispatch(self, cmd: dict) -> None:
        t = cmd.get("type", "")
        handler = {
            "servo":     self._h_servo,
            "relay":     self._h_relay,
            "gpio":      self._h_gpio,
            "fas":       self._h_fas,
            "console":   self._h_console,
            "data_file": self._h_data_file,
        }.get(t)
        if handler:
            handler(cmd)
        else:
            self._log(2, f"[dummy] unhandled type={t!r}")

    def _h_servo(self, cmd: dict) -> None:
        ch    = int(cmd.get("id", 0))
        angle = cmd.get("angle", 0)
        with self._lock:
            self._servo_states[ch] = angle
        self._log(1, f"[dummy] servo ch={ch} angle={angle}")

    def _h_relay(self, cmd: dict) -> None:
        ch    = int(cmd.get("id", 0))
        state = bool(cmd.get("state", 0))
        with self._lock:
            if 0 <= ch < 16:
                self._relay_states[ch] = state
        self._log(1, f"[dummy] relay ch={ch} {'ON' if state else 'OFF'}")

    def _h_gpio(self, cmd: dict) -> None:
        pin   = int(cmd.get("id", 0))
        state = int(cmd.get("state", 0))
        with self._lock:
            self._gpio_states[pin] = state
        self._log(1, f"[dummy] gpio pin={pin} state={state}")

    def _h_fas(self, cmd: dict) -> None:
        if "port" in cmd:
            self._fas_port(cmd)
        elif "op" in cmd:
            self._fas_op(cmd)
        else:
            self._log(2, f"[dummy] fas: unrecognised shape {cmd}")

    def _fas_port(self, cmd: dict) -> None:
        board_id = _resolve_fas_board_id(cmd)
        channel  = int(cmd.get("channel", 0))
        port     = str(cmd.get("port", ""))
        action   = str(cmd.get("action", "")).lower()

        if port == "relay":
            enable = action == "on"
            self._log(1, f"[dummy] fas load_sw EPB:{board_id} ch={channel} "
                         f"{'ON' if enable else 'OFF'}")

        elif port == "servo":
            if action == "enable":
                # enable is a no-op at the wire level
                self._log(2, f"[dummy] fas servo enable (no-op) EPB:{board_id} ch={channel}")
                return
            pulse_us = 0 if action == "disable" else int(cmd.get("value", 0))
            self._log(1, f"[dummy] fas pwm_set EPB:{board_id} ch={channel} "
                         f"pulse={pulse_us}µs")

        elif port == "gpio":
            act = action.upper()
            if act == "ARM":
                self._log(1, f"[dummy] fas imc_arm EPB:{board_id}")
                with self._lock:
                    self._fas_imc.update({"board_id": board_id, "arm_line": True})
            elif act == "DISARM":
                self._log(1, f"[dummy] fas imc_disarm EPB:{board_id}")
                with self._lock:
                    self._fas_imc.update({"board_id": board_id,
                                          "arm_line": False, "disarm_line": True,
                                          "armed": False})
            else:
                self._log(2, f"[dummy] fas gpio: unknown action {action!r}")
        else:
            self._log(2, f"[dummy] fas: unknown port {port!r}")

    def _fas_op(self, cmd: dict) -> None:
        op       = str(cmd.get("op", ""))
        board_id = int(cmd.get("board_id", 0))
        channel  = int(cmd.get("channel", 0))

        if op == "pwm_set":
            pulse_us  = int(cmd.get("pulse_us", 0))
            period_us = int(cmd.get("period_us", 20000))
            self._log(1, f"[dummy] fas pwm_set EPB:{board_id} ch={channel} "
                         f"pulse={pulse_us}µs period={period_us}µs")

        elif op == "load_sw_set":
            enable  = bool(cmd.get("enable", False))
            hold_ms = int(cmd.get("hold_ms", 0))
            self._log(1, f"[dummy] fas load_sw EPB:{board_id} ch={channel} "
                         f"enable={enable} hold={hold_ms}ms")

        elif op == "failsafe":
            self._log(1, f"[dummy] fas failsafe EPB:{board_id}")

        elif op == "imc_arm":
            pulse_ms = int(cmd.get("pulse_ms", 0))
            self._log(1, f"[dummy] fas imc_arm EPB:{board_id} pulse={pulse_ms}ms")
            with self._lock:
                self._fas_imc.update({"board_id": board_id, "arm_line": True})

        elif op == "imc_disarm":
            pulse_ms = int(cmd.get("pulse_ms", 0))
            self._log(1, f"[dummy] fas imc_disarm EPB:{board_id} pulse={pulse_ms}ms")
            with self._lock:
                self._fas_imc.update({"board_id": board_id,
                                      "arm_line": False, "disarm_line": True,
                                      "armed": False})

        elif op == "discover":
            self._log(2, "[dummy] fas discover (boards already online)")

        elif op == "actuator_query":
            self._log(1, f"[dummy] fas actuator_query EPB:{board_id} ch={channel}")

        else:
            self._log(2, f"[dummy] fas: unknown op {op!r}")

    def _h_console(self, cmd: dict) -> None:
        action = str(cmd.get("action", "")).lower()
        with self._lock:
            self._console_active = action == "start"
        self._log(1, f"[dummy] console {'started' if self._console_active else 'stopped'}")

    def _h_data_file(self, cmd: dict) -> None:
        self._log(1, f"[dummy] data_file: {cmd}")

    # ── Telemetry loop ───────────────────────────────────────────────────────

    def _publish_loop(self) -> None:
        session_ms = 0
        while True:
            now_s     = time.monotonic() - self._t0
            now_ms    = int(now_s * 1000)
            session_ms += self._publish_ms

            # GCS ADC: slow-drifting sine waves per channel (hat_id/channel_id keyed)
            gcs_sensors: list[dict] = []
            for hat in MOCK_HATS:
                for ch in range(hat["channels"]):
                    freq  = 0.3 + hat["hat_id"] * 0.2 + ch * 0.07
                    value = 3 + math.sin(2 * math.pi * freq * now_s) * 2.5 + random.gauss(0, 0.008)
                    gcs_sensors.append({
                        "hat_id":     hat["hat_id"],
                        "channel_id": ch,
                        "value":      round(value, 5),
                        "timestamp":  now_ms,
                    })

            # FAS ADC: node/channel keyed to match config sensor bindings
            fas_sensors: list[dict] = []
            for bid in MOCK_EPB_IDS:
                node = f"EPB_{bid + 1}"
                for ch in range(2):
                    raw_code = random.gauss(600, 25)
                    fas_sensors.append({
                        "node":      node,
                        "channel":   ch,
                        "value":     round(raw_code * ADC_INT16_TO_V, 6),
                        "timestamp": now_ms,
                    })

            with self._lock:
                gpio_snap    = dict(self._gpio_states)
                fas_imc_snap = dict(self._fas_imc)
                console      = self._console_active

            gpios = [{"pin_id": pin, "state": st} for pin, st in gpio_snap.items()]

            fas_boards = [
                {"key": f"EPB:{bid}", "online": True, "uptime_ms": session_ms}
                for bid in MOCK_EPB_IDS
            ]

            engine_payload = {
                "source":  self._node_id,
                "sensors": gcs_sensors,
                "gpios":   gpios,
            }
            fas_sensor_payload = {
                "source":  "FAS",
                "sensors": fas_sensors,
            }
            flight_payload = {
                "source":     self._node_id,
                "fas_boards": fas_boards,
                "fas_imc":    fas_imc_snap,
            }
            self._client.publish(TELEMETRY_TOPIC, json.dumps(engine_payload),    qos=0)
            self._client.publish(TELEMETRY_TOPIC, json.dumps(fas_sensor_payload), qos=0)
            self._client.publish(FLIGHT_TOPIC,    json.dumps(flight_payload),     qos=0)

            # Emit a simulated FAS heartbeat frame to nova/console when active
            if console:
                uptime_bytes = session_ms.to_bytes(4, "little")
                data_hex = " ".join(f"{b:02x}" for b in uptime_bytes) + " 00 00 00 00"
                frame = {
                    "source":     "novaGround",
                    "type":       "fas_frame",
                    "can_id":     _can_id_pack(0x01, 2, 0, 0, 0, session_ms & 0xFF),
                    "msg_type":   0x01,   # HEARTBEAT
                    "board_kind": 2,       # EPB
                    "board_id":   0,
                    "channel":    0,
                    "data_hex":   data_hex,
                }
                self._client.publish(CONSOLE_TOPIC, json.dumps(frame), qos=0)

            time.sleep(self._publish_ms / 1000.0)

    def _log(self, level: int, msg: str) -> None:
        if self._verbosity >= level:
            print(msg, flush=True)

    def run(self) -> None:
        threading.Thread(target=self._publish_loop, daemon=True).start()
        self._client.loop_forever()


# ── Helpers ──────────────────────────────────────────────────────────────────

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


def _can_id_pack(msg: int, kind: int, board_id: int, channel: int,
                 flags: int, seq: int) -> int:
    return (((msg & 0x3F) << 23) | ((kind & 0x07) << 20)
            | ((board_id & 0x07) << 17) | ((channel & 0x3F) << 11)
            | ((flags & 0x07) << 8) | (seq & 0xFF))


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Simulated novaGround MQTT client")
    p.add_argument("--broker",     default="localhost:1883",
                   help="MQTT broker address (default localhost:1883)")
    p.add_argument("--node-id",    default="nova_dummy",
                   help="MQTT client ID / telemetry source name")
    p.add_argument("--publish-ms", type=int, default=50,
                   help="Telemetry publish interval in ms (default 50)")
    p.add_argument("--verbosity",  type=int, default=1, choices=[0, 1, 2],
                   help="0=quiet  1=commands  2=debug")
    args = p.parse_args()

    NovaDummy(
        broker=args.broker,
        node_id=args.node_id,
        publish_ms=args.publish_ms,
        verbosity=args.verbosity,
    ).run()


if __name__ == "__main__":
    main()
