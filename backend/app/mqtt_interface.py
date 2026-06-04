import paho.mqtt.client as mqtt
from fastapi import HTTPException
import json
import os
from datetime import datetime
import asyncio
import time
import config_parser
import data_interface

MQTT_BROKER = os.getenv("MQTT_BROKER", "host.docker.internal") # use broker.hivemq.com for testing on PCs
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883")) # TCP Port
DATA_TOPIC = "novaground/telemetry"
COMMAND_TOPIC = "novaground/command"
UART_TOPIC = "novaground/uart"

raw_data = {}
raw_uart_data = {}
processed_data = {"sensors": [], "actuators": [], "gpios": []}
data_store = []
processed_gpios = []


# MQTT client setup
mqtt_client = mqtt.Client()
mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected to MQTT broker")
        client.subscribe(DATA_TOPIC)
        client.subscribe(UART_TOPIC)
    else:
        print("Failed to connect to MQTT broker")

def on_message(client, userdata, msg):
    global raw_data
    global raw_uart_data
    global processed_data
    # Decode the payload from bytes to string
    payload_str = msg.payload.decode('utf-8', errors='replace').strip()

    # Debugging: print the raw payload
    # print(f"Received payload: {payload_str}")
    try:
        # Check if the payload is non-empty before attempting to decode as JSON
        if payload_str:
            payload = json.loads(payload_str)  # Attempt to decode the payload into JSON
            # print(f"Decoded data: {data}"
            # Make sure payload is a dictionary before using it in the process
            if isinstance(payload, dict):
                if msg.topic == UART_TOPIC:
                    raw_uart_data = payload
                    if not isinstance(raw_data, dict):
                        raw_data = {}
                    raw_data["uart"] = payload
                    asyncio.run(data_interface.process_uart_data(payload))
                else:
                    raw_data = payload
                    asyncio.run(data_interface.process_data(payload))
            else:
                print("Received payload is not a valid dictionary")
        else:
            print("Received empty payload.")
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON: {e} - Payload: {msg.payload.decode('utf-8')}")
    except Exception as e:
        print(f"Unexpected error: {e}")

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
try:
    mqtt_client.connect_async(MQTT_BROKER, MQTT_PORT)
except Exception as e:
    print(f"MQTT initial connection setup failed: {e}")

# Start the MQTT loop. connect_async will retry in the background.
mqtt_client.loop_start()

async def process_mqtt_message(payload):
    # Make sure payload is a dictionary before using it in the process
    if isinstance(payload, dict):
        data_store["sensors"] = payload.get("sensors", [])
        data_store["actuators"] = payload.get("actuators", [])
    else:
        print("Received payload is not a valid dictionary")

async def publish_command(command):
    try:
        mqtt_client.publish(COMMAND_TOPIC, json.dumps(command))
        return {"status": "Command sent"}
    except Exception as e:
        print(f"Error publishing command: {e} - Payload: {json.dumps(command)}")
        raise Exception(f"Error publishing command: {e} - Payload: {json.dumps(command)}")
    
