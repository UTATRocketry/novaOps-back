import paho.mqtt.client as mqtt
from fastapi import HTTPException
import json
from datetime import datetime
import asyncio

MQTT_BROKER = "mqtt://localhost:1883" # use broker.hivemq.com for testing on PCs
MQTT_PORT = 1883 # TCP Port
DATA_TOPIC = "novaground/telemetry"
COMMAND_TOPIC = "novaground/command"

data_store = {"sensors": [], "actuators": []}

# MQTT client setup
mqtt_client = mqtt.Client()

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected to MQTT broker")
        client.subscribe(DATA_TOPIC)
    else:
        print("Failed to connect to MQTT broker")

def on_message(client, userdata, msg):
    # Decode the payload from bytes to string
    payload_str = msg.payload.decode('utf-8', errors='replace').strip()

    # Debugging: print the raw payload
    # print(f"Received payload: {payload_str}")
    try:
        # Check if the payload is non-empty before attempting to decode as JSON
        if payload_str:
            data = json.loads(payload_str)  # Attempt to decode the payload into JSON
            # print(f"Decoded data: {data}")
            asyncio.run(process_mqtt_message(data))
        else:
            print("Received empty payload.")
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON: {e} - Payload: {msg.payload.decode('utf-8')}")
    except Exception as e:
        print(f"Unexpected error: {e}")

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.connect(MQTT_BROKER, MQTT_PORT)

# Start the MQTT loop
mqtt_client.loop_start()

async def process_mqtt_message(payload):
    # Make sure payload is a dictionary before using it in the process
    if isinstance(payload, dict):
        data_store["sensors"] = payload.get("sensors", [])
        data_store["actuators"] = payload.get("actuators", [])
    else:
        print("Received payload is not a valid dictionary")