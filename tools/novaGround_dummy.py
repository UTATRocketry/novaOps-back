"""
novaGround simulator — publishes fake GCS sensor data to nova/telemetry/engine
and listens for commands on nova/command and data-saving control on nova/control.

Usage:
    python tools/novaGround_dummy.py

Environment variables:
    NOVA_MQTT_BROKER   MQTT broker host (default: localhost)
    NOVA_MQTT_PORT     MQTT broker port (default: 1883)
    NOVA_PUBLISH_HZ    Publish rate in Hz (default: 20)
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

TELEMETRY_TOPIC = "nova/telemetry/engine"
COMMAND_TOPIC = "nova/command"
CONTROL_TOPIC = "nova/control"
CLIENT_ID = "novaGround_dummy"

# HAT/channel layout — mirrors the bindings in config/system.yaml
# hat_id=0: PGSO(ch0), PGS(ch1), MOT(ch2), MFT(ch7)
# hat_id=1: CC-LC(ch5)
GCS_SENSORS = [
    {"hat_id": 0, "channel_id": 0, "label": "PGSO"},  # pressure 0–775 psi range
    {"hat_id": 0, "channel_id": 1, "label": "PGS"},
    {"hat_id": 0, "channel_id": 2, "label": "MOT"},   # load cell
    {"hat_id": 0, "channel_id": 7, "label": "MFT"},   # load cell
    {"hat_id": 1, "channel_id": 5, "label": "CC-LC"},
]


def _sim_voltage(label: str, t: float) -> float:
    """Return a plausible raw voltage for the given sensor name."""
    # Pressure sensors: 0.99–4.08 V represents 0–775 psi
    if label in {"PGSO", "PGS"}:
        return 0.99 + 1.55 * (0.5 + 0.5 * math.sin(t * 0.3)) + random.uniform(-0.02, 0.02)
    # Load cells: 3.59–5.2 V
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


def on_connect(client: mqtt.Client, _userdata, _flags, reason_code, _properties) -> None:
    ok = reason_code == 0 or (hasattr(reason_code, "is_failure") and not reason_code.is_failure)
    if ok:
        client.subscribe(COMMAND_TOPIC)
        client.subscribe(CONTROL_TOPIC)
        print(f"[novaGround] Connected to {BROKER}:{PORT}")
        print(f"[novaGround] Subscribed to {COMMAND_TOPIC} and {CONTROL_TOPIC}")
    else:
        print(f"[novaGround] Connect failed: {reason_code}")


def on_disconnect(_client, _userdata, _flags, reason_code, _properties) -> None:
    print(f"[novaGround] Disconnected: {reason_code}")


def on_message(_client, _userdata, message) -> None:
    try:
        payload = json.loads(message.payload.decode("utf-8"))
        topic = message.topic
        cmd = payload.get("command", {})
        cmd_type = cmd.get("type", "")

        if topic == CONTROL_TOPIC:
            # Physical lockout from novaLock, or data-saving echo from novaOps
            source = payload.get("source", "")
            if source.lower() == "novalock":
                state = payload.get("state", "?")
                print(f"[novaGround] Physical lockout: {state}")
            elif cmd_type == "data_file":
                _handle_data_file(cmd)
            else:
                print(f"[novaGround] Control message: {payload}")
            return

        # nova/command
        source = payload.get("source", "")
        if source != "novaOps":
            return  # ignore our own or unrecognised sources

        if cmd_type == "relay":
            print(f"[novaGround] Relay command — id={cmd.get('id')} state={cmd.get('state')}")
        elif cmd_type == "servo":
            print(f"[novaGround] Servo command — id={cmd.get('id')} angle={cmd.get('angle')}")
        elif cmd_type == "gpio":
            print(f"[novaGround] GPIO command — id={cmd.get('id')} state={cmd.get('state')}")
        elif cmd_type == "data_file":
            _handle_data_file(cmd)
        else:
            print(f"[novaGround] Unknown command type '{cmd_type}': {cmd}")

    except Exception as exc:
        print(f"[novaGround] Error processing message: {exc}")


def _handle_data_file(cmd: dict) -> None:
    action = cmd.get("action", "")
    if action == "start_data_saving":
        filename = cmd.get("filename", "unknown")
        print(f"[novaGround] Data saving STARTED — file: {filename}.csv")
    elif action == "stop_data_saving":
        print("[novaGround] Data saving STOPPED")
    else:
        print(f"[novaGround] Unknown data_file action: {action}")


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

    interval = 1.0 / HZ
    print(f"[novaGround] Publishing to {TELEMETRY_TOPIC} at {HZ} Hz")

    try:
        while True:
            packet = build_gcs_packet()
            client.publish(TELEMETRY_TOPIC, json.dumps(packet), qos=0)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[novaGround] Stopping")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
