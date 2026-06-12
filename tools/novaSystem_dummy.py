"""
novaGround + FAS simulator.

Does everything novaGround_dummy.py does (publishes fake GCS engine sensor data
and reacts to commands on nova/command / nova/control) and ALSO emulates the
Flight Avionics System (FAS):

Publishes:
    nova/telemetry/engine  — GCS sensor packets (source="novaGround") AND
                             FAS node/channel sensor packets (source="FAS")
    nova/telemetry/flight  — flight telemetry (data + events), shaped after the
                             FAS_Data_t / FAS_Event_t structs in FAS/flight.h
    nova/console           — occasional console/log lines

Listens on nova/command and nova/control:
    relay / servo / gpio   — GCS device commands
    type "fas"             — FAS device commands (relay/servo/gpio) keyed by node
    type "fas_cmd"         — FAS system commands (flight state, camera, ...) keyed by node
    type "data_file"       — data-saving start/stop

Usage:
    python tools/novaFAS_dummy.py

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
CLIENT_ID = "novaFAS_dummy"

# ---------------------------------------------------------------------------
# GCS engine sensors — mirrors the bindings in config/system.yaml.
# hat_id=0: PGSO(ch0), PGS(ch1), MOT(ch2), MFT(ch7); hat_id=1: CC-LC(ch5)
# ---------------------------------------------------------------------------
GCS_SENSORS = [
    {"hat_id": 0, "channel_id": 0, "label": "PGSO"},
    {"hat_id": 0, "channel_id": 1, "label": "PGS"},
    {"hat_id": 0, "channel_id": 2, "label": "MOT"},
    {"hat_id": 0, "channel_id": 7, "label": "MFT"},
    {"hat_id": 1, "channel_id": 5, "label": "CC-LC"},
]

# FAS node/channel sensors — PFT(EPB_1/ch1), POT(EPB_2/ch1), PCC(EPB_3/ch1).
# Each maps onto the matching EPB's analog "sensor" reading.
FAS_SENSORS = [
    {"node": "EPB_1", "channel": 1, "label": "PFT"},
    {"node": "EPB_2", "channel": 1, "label": "POT"},
    {"node": "EPB_3", "channel": 1, "label": "PCC"},
]

# Flight states from Flight_State_t / lookupTableFlightState in flight.h.
FLIGHT_STATES = [
    "INIT", "STANDBY", "ARMED", "POWERED_ASCENT",
    "COASTING", "APOGEE", "DESCENT", "LANDED",
]
EVENT_SEVERITY = ["DEBUG", "INFO", "WARNING", "ERROR", "FATAL"]

# ---------------------------------------------------------------------------
# Mutable sim state
# ---------------------------------------------------------------------------
_boot_time = time.time()
_flight_state = "STANDBY"
_raven_armed = False
_strato_armed = False
_imc_armed = False
_event_queue: list[dict] = []


def _push_event(name: str, severity: str = "INFO") -> None:
    _event_queue.append(
        {
            "name": name,
            "severity": severity,
            "timestamp": int(time.time() * 1000),
        }
    )
    if len(_event_queue) > 20:
        del _event_queue[: len(_event_queue) - 20]


# ---------------------------------------------------------------------------
# GCS engine sensor simulation (identical behavior to novaGround_dummy.py)
# ---------------------------------------------------------------------------
def _sim_voltage(label: str, t: float) -> float:
    if label in {"PGSO", "PGS"}:
        return 0.99 + 1.55 * (0.5 + 0.5 * math.sin(t * 0.3)) + random.uniform(-0.02, 0.02)
    if label in {"MOT", "MFT", "CC-LC"}:
        return 3.59 + 0.8 * abs(math.sin(t * 0.1)) + random.uniform(-0.05, 0.05)
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
# FAS sensor simulation (analog EPB sensor voltages, on the engine topic)
# ---------------------------------------------------------------------------
def _sim_epb_sensor(label: str, t: float) -> float:
    """Raw analog volts for an EPB-backed pressure transducer."""
    base = {"PFT": 2.4, "POT": 2.2, "PCC": 1.0}.get(label, 1.5)
    swing = {"PFT": 0.6, "POT": 0.5, "PCC": 0.2}.get(label, 0.3)
    return base + swing * (0.5 + 0.5 * math.sin(t * 0.25)) + random.uniform(-0.01, 0.01)


def build_fas_sensor_packet() -> dict:
    t = time.time()
    timestamp = int(t * 1000)
    return {
        "source": "FAS",
        "sensors": [
            {
                "node": s["node"],
                "channel": s["channel"],
                "value": round(_sim_epb_sensor(s["label"], t), 4),
                "timestamp": timestamp,
            }
            for s in FAS_SENSORS
        ],
    }


# ---------------------------------------------------------------------------
# Flight telemetry — flattened key/value view of FAS_Data_t (flight.h)
# ---------------------------------------------------------------------------
def _epb_block(prefix: str, label: str, t: float) -> dict:
    return {
        f"{prefix}.current": round(120.0 + 40.0 * abs(math.sin(t * 0.2)) + random.uniform(-2, 2), 2),
        f"{prefix}.voltage": round(8.3 + random.uniform(-0.05, 0.05), 3),
        f"{prefix}.sensor": round(_sim_epb_sensor(label, t), 4),
    }


def build_flight_packet() -> dict:
    global _event_queue
    t = time.time()
    uptime = t - _boot_time
    ascent = _flight_state in {"POWERED_ASCENT", "COASTING", "APOGEE", "DESCENT"}

    data = {
        "timestamp": int(t * 1000),
        "fas.flightState": _flight_state,
        # FAS_Status_t flags
        "fas.armingStatus": 1 if _flight_state in {"ARMED", "POWERED_ASCENT"} else 0,
        "fas.telemetryStatus": 1,
        "fas.sensorsStatus": 1,
        "fas.commsStatus": 1,
        "fas.engineStatus": 1 if ascent else 0,
        "fas.recoveryStatus": 1 if _flight_state in {"APOGEE", "DESCENT", "LANDED"} else 0,
        # FMC_Data_t
        "fmc.accel.x": round(random.uniform(-0.2, 0.2), 3),
        "fmc.accel.y": round(random.uniform(-0.2, 0.2), 3),
        "fmc.accel.z": round((40.0 if _flight_state == "POWERED_ASCENT" else 9.81) + random.uniform(-0.1, 0.1), 3),
        "fmc.gyro.x": round(random.uniform(-5, 5), 2),
        "fmc.gyro.y": round(random.uniform(-5, 5), 2),
        "fmc.gyro.z": round(random.uniform(-5, 5), 2),
        "fmc.pressureBaro": round(14.7 - (0.0004 * min(uptime, 6000) if ascent else 0.0) + random.uniform(-0.02, 0.02), 4),
        "fmc.altitudeBaro": round(max(0.0, (uptime * 30.0) if ascent else 0.0) + random.uniform(-1, 1), 2),
        "fmc.tempBaro": round(24.0 + random.uniform(-0.3, 0.3), 2),
        "fmc.longitude": round(-117.8443 + random.uniform(-1e-4, 1e-4), 6),
        "fmc.latitude": round(34.0561 + random.uniform(-1e-4, 1e-4), 6),
        "fmc.altitudeGPS": round(max(0.0, (uptime * 30.0) if ascent else 0.0), 1),
        "fmc.numSatilites": random.randint(7, 12),
        "fmc.tempH7": round(31.0 + 4.0 * math.sin(t * 0.1) + random.uniform(-0.2, 0.2), 2),
        "fmc.tempPWR": round(36.0 + random.uniform(-0.4, 0.4), 2),
        # PMB_Data_t
        "pmb.currentMain": round(800.0 + random.uniform(-20, 20), 1),
        "pmb.current8V4": round(420.0 + random.uniform(-10, 10), 1),
        "pmb.current24V": round(150.0 + random.uniform(-8, 8), 1),
        "pmb.voltageBatt": round(16.6 - 0.0005 * uptime + random.uniform(-0.02, 0.02), 3),
        "pmb.voltage8V4": round(8.4 + random.uniform(-0.03, 0.03), 3),
        "pmb.voltage24V": round(24.0 + random.uniform(-0.05, 0.05), 3),
        "pmb.tempBatt": round(28.0 + random.uniform(-0.5, 0.5), 2),
        "pmb.tempBuck": round(40.0 + random.uniform(-1, 1), 2),
        "pmb.tempAmb": round(23.0 + random.uniform(-0.5, 0.5), 2),
        # RAB_Data_t x2
        "rab1.armed": int(_raven_armed),
        "rab1.voltageBatt": round(9.1 + random.uniform(-0.05, 0.05), 3),
        "rab2.armed": int(_strato_armed),
        "rab2.voltageBatt": round(9.0 + random.uniform(-0.05, 0.05), 3),
        # IMC arming (gpio_device IMC-V)
        "fas.imcArmed": int(_imc_armed),
    }
    # EPB_Data_t x3
    data.update(_epb_block("epb1", "PFT", t))
    data.update(_epb_block("epb2", "POT", t))
    data.update(_epb_block("epb3", "PCC", t))

    # Drain queued events, always include a heartbeat.
    events = list(_event_queue)
    _event_queue = []
    events.append({"name": "HEARTBEAT", "severity": "DEBUG", "state": _flight_state, "timestamp": int(t * 1000)})

    return {"data": data, "events": events}


# ---------------------------------------------------------------------------
# MQTT plumbing
# ---------------------------------------------------------------------------
def on_connect(client: mqtt.Client, _userdata, _flags, reason_code, _properties) -> None:
    ok = reason_code == 0 or (hasattr(reason_code, "is_failure") and not reason_code.is_failure)
    if ok:
        client.subscribe(COMMAND_TOPIC)
        client.subscribe(CONTROL_TOPIC)
        print(f"[novaFAS] Connected to {BROKER}:{PORT}")
        print(f"[novaFAS] Subscribed to {COMMAND_TOPIC} and {CONTROL_TOPIC}")
        _push_event("GCS_CONNECT", "INFO")
        _push_event("SYS_STARTUP", "INFO")
    else:
        print(f"[novaFAS] Connect failed: {reason_code}")


def on_disconnect(_client, _userdata, _flags, reason_code, _properties) -> None:
    print(f"[novaFAS] Disconnected: {reason_code}")


def on_message(_client, _userdata, message) -> None:
    global _flight_state, _raven_armed, _strato_armed, _imc_armed
    try:
        payload = json.loads(message.payload.decode("utf-8"))
        topic = message.topic
        cmd = payload.get("command", {})
        cmd_type = cmd.get("type", "")

        if topic == CONTROL_TOPIC:
            source = payload.get("source", "")
            if source.lower() == "novalock":
                print(f"[novaFAS] Physical lockout: {payload.get('state', '?')}")
            elif cmd_type == "data_file":
                _handle_data_file(cmd)
            else:
                print(f"[novaFAS] Control message: {payload}")
            return

        # nova/command
        if payload.get("source", "") != "novaOps":
            return

        # --- GCS device commands (same as novaGround_dummy.py) ---
        if cmd_type == "relay":
            print(f"[novaFAS] Relay command — id={cmd.get('id')} state={cmd.get('state')}")
        elif cmd_type == "servo":
            print(f"[novaFAS] Servo command — id={cmd.get('id')} angle={cmd.get('angle')}")
        elif cmd_type == "gpio":
            print(f"[novaFAS] GPIO command — id={cmd.get('id')} state={cmd.get('state')}")

        # --- FAS device commands ---
        elif cmd_type == "fas":
            value = cmd.get("value")
            extra = f" value={value}" if value is not None else ""
            print(
                f"[novaFAS] FAS device — node={cmd.get('node')} "
                f"{cmd.get('port')}[{cmd.get('channel')}] action={cmd.get('action')}{extra}"
            )
            if cmd.get("port") == "gpio" and cmd.get("node") == "EPB_3" and cmd.get("channel") == 2:
                action = str(cmd.get("action", "")).strip().lower()
                _imc_armed = action in {"arm", "armed", "on", "1", "true"}
                _push_event("IMC_IS_ARMED" if _imc_armed else "IMC_NOT_ARMED", "INFO")
            _push_event("EPB_COMMAND_FAILED" if cmd.get("port") is None else "_CMD_RECEIVED", "DEBUG")

        # --- FAS system commands ---
        elif cmd_type == "fas_cmd":
            name = cmd.get("command")
            state = cmd.get("state")
            node = cmd.get("node")
            _apply_system_command(name, state)
            suffix = f" state={state}" if state is not None else ""
            print(f"[novaFAS] FAS system — node={node} {name}{suffix}")

        elif cmd_type == "data_file":
            _handle_data_file(cmd)
        else:
            print(f"[novaFAS] Unhandled command type '{cmd_type}': {cmd}")

    except Exception as exc:
        print(f"[novaFAS] Error processing message: {exc}")


def _apply_system_command(name: str, state) -> None:
    global _flight_state, _raven_armed, _strato_armed, _imc_armed
    if name == "SET_FLIGHT_STATE" and state in FLIGHT_STATES:
        _flight_state = state
        _push_event("ARM_FMC_COMMAND_RECEIVED" if state == "ARMED" else "DISARM_FMC_COMMAND_RECEIVED", "INFO")
    elif name == "SET_BLUE_RAVEN_STATE":
        _raven_armed = state == "ARM"
        _push_event("RAVEN_IS_ARMED" if _raven_armed else "RAVEN_NOT_ARMED", "INFO")
    elif name == "SET_STRATO_STATE":
        _strato_armed = state == "ARM"
        _push_event("STRATO_IS_ARMED" if _strato_armed else "STRATO_NOT_ARMED", "INFO")
    elif name in {"START_CAMERA_RECORDING", "STOP_CAMERA_RECORDING"}:
        _push_event(name, "INFO")
    elif name == "POWER_CYCLE_SOFT":
        _push_event("SYS_REBOOT", "WARNING")


def _handle_data_file(cmd: dict) -> None:
    action = cmd.get("action", "")
    if action == "start_data_saving":
        print(f"[novaFAS] Data saving STARTED — file: {cmd.get('filename', 'unknown')}.csv")
    elif action == "stop_data_saving":
        print("[novaFAS] Data saving STOPPED")
    else:
        print(f"[novaFAS] Unknown data_file action: {action}")


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
    print(f"[novaFAS] Publishing GCS + FAS sensors to {TELEMETRY_TOPIC} at {HZ} Hz")
    print(f"[novaFAS] Publishing flight telemetry to {FLIGHT_TOPIC} at {FLIGHT_HZ} Hz")

    try:
        while True:
            now = time.time()
            client.publish(TELEMETRY_TOPIC, json.dumps(build_gcs_packet()), qos=0)
            client.publish(TELEMETRY_TOPIC, json.dumps(build_fas_sensor_packet()), qos=0)
            if now >= next_flight:
                client.publish(FLIGHT_TOPIC, json.dumps(build_flight_packet()), qos=0)
                next_flight = now + flight_interval
            time.sleep(sensor_interval)
    except KeyboardInterrupt:
        print("\n[novaFAS] Stopping")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
