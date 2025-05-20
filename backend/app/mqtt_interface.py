import paho.mqtt.client as mqtt
from fastapi import HTTPException
import json
from datetime import datetime
import asyncio
from config_parser import get_config, process_data, convert_command
MQTT_BROKER = "host.docker.internal" # use broker.hivemq.com for testing on PCs
MQTT_PORT = 1883 # TCP Port
DATA_TOPIC = "novaground/telemetry"
COMMAND_TOPIC = "novaground/command"

raw_data = {}
data_store = {"sensors": [], "actuators": []}
processed_data = {}
# MQTT client setup
mqtt_client = mqtt.Client()

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected to MQTT broker")
        client.subscribe(DATA_TOPIC)
    else:
        print("Failed to connect to MQTT broker")

def on_message(client, userdata, msg):
    global raw_data
    global processed_data
    # Decode the payload from bytes to string
    payload_str = msg.payload.decode('utf-8', errors='replace').strip()

    # Debugging: print the raw payload
    # print(f"Received payload: {payload_str}")
    try:
        # Check if the payload is non-empty before attempting to decode as JSON
        if payload_str:
            raw_data = json.loads(payload_str)  # Attempt to decode the payload into JSON
            # print(f"Decoded data: {data}"
            # Make sure payload is a dictionary before using it in the process
            if isinstance(raw_data, dict):
                processed_data = asyncio.run(process_data(raw_data))
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

async def publish_command(command):
    try:
        mqtt_client.publish(mqtt.COMMAND_TOPIC, json.dumps(command))
        return {"status": "Command sent"}
    except Exception as e:
        print(f"Error publishing command: {e} - Payload: {json.dumps(command)}")
        raise Exception(f"Error publishing command: {e} - Payload: {json.dumps(command)}")
    

async def set_all_to_closed():
    """Set all servos and solenoids to their closed state."""
    config = get_config()
    commands = []
    # Send commands to close all solenoids
    for relay in config["relays"].values():
        if relay.get("type") is None:
            continue
        command = {
            "type": "solenoid",
            "name": relay["name"],
            "state": "closed"
        }
        relay_commands = await convert_command(command)
        commands.extend(relay_commands)
    # Send commands to close all servos
    for servo in config["servos"].values():
        command = {
            "type": "servo",
            "name": servo["name"],
            "state": "closed"
        }
        servo_commands = await convert_command(command)
        commands.extend(servo_commands)
        
    # Publish all commands to the MQTT broker
    for command in commands:
        await publish_command(command)