import paho.mqtt.client as mqtt
from fastapi import HTTPException
import json
from datetime import datetime
import asyncio
import time
from config_parser import get_config, process_data, convert_command, save_data, SAVE_DATA_FLAG
MQTT_BROKER = "host.docker.internal" # use broker.hivemq.com for testing on PCs
MQTT_PORT = 1883 # TCP Port
DATA_TOPIC = "novaground/telemetry"
COMMAND_TOPIC = "novaground/command"

raw_data = {}
data_store = {"sensors": [], "actuators": []}
processed_data = {"sensors": [], "actuators": [], "gpios": []}
processed_gpios = []
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
                if "sensors" in raw_data:
                    processed_data["sensors"] = asyncio.run(process_data(raw_data, "sensors"))
                    
                if "gpios" in raw_data:
                    processed_data["gpios"] = asyncio.run(process_data(raw_data, "gpios"))

                if "actuators" in raw_data:
                    processed_data["actuators"] = asyncio.run(process_data(raw_data, "actuators"))

                if "thermocouples" in raw_data:
                    processed_data["thermocouples"] = asyncio.run(process_data(raw_data, "thermocouples"))

                if SAVE_DATA_FLAG:
                    save_data(processed_data)  # Save the processed data to a file
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
    """
    # Send commands to close all solenoids
    for relay in config["relays"].values():
        if relay.get("type") is None:
            continue
        command = {
            "type": "relay",
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
        time.sleep(0.1)  # Add a small delay between commands to avoid flooding the broker
    """
    for i in range(16):
        command = {
            "type": "relay",
            "id": i,
            "state": 1
        }
        mqtt_client.publish(mqtt.COMMAND_TOPIC, json.dumps(command))
        time.sleep(0.1)  # Add a small delay between commands to avoid flooding the broker
    return {"status": "Commands sent"}

async def set_to_defaults():
    """Set all actuators to their default state."""
    config = get_config()
    commands = []
    wait = {"wait"}
    for servo in config["servos"].values():
        # Send commands to set all servos to their default state
        name = servo["name"]
        default_state = servo["default_state"]

        if default_state is None:
            raise ValueError(f"Default state not defined for servo '{name}'.")
                
        if default_state == "open":
            angle = servo["open_pos"]
        elif default_state == "closed":
            angle = servo["close_pos"]
            
        command = {
        "type": "servo",
        "id": servo["channelID"],
        "angle": angle
        }
        if "relayID" in servo:
            power_on = {
                "type": "relay",
                "id": servo["relayID"],
                "state": 0
            }
            power_off = {
                "type": "relay",
                "id": servo["relayID"],
                "state": 1
            }
        
        if power_on: 
            commands.append(power_on)
            commands.append(wait)
        
        commands.append(command)

        if power_off: 
            commands.append(wait)
            commands.append(power_off)
        
    for gpio in config["gpios"].values():
        # Send commands to set all GPIOs to their default state
        name = gpio["name"]
        default_state = gpio["default_state"]

        if default_state is None:
            raise ValueError(f"Default state not defined for GPIO '{name}'.")

        if default_state == "armed":
            state = 1
        elif default_state == "disarmed":
            state = 0
        
        if "relayID" in gpio:
            power_on = {
                "type": "relay",
                "id": gpio["relayID"],
                "state": 0
            }
            power_off = {
                "type": "relay",
                "id": gpio["relayID"],
                "state": 1
            }
        command = {
            "type": "gpio",
            "id": gpio["channelID"],
            "state": state
        }
        if power_on: 
            commands.append(power_on)
            commands.append(wait)
        
        commands.append(command)

        if power_off: 
            commands.append(wait)
            commands.append(power_off)

    for relay in config["relays"].values():
        # Send commands to set all solenoids to their default state
        if relay["actuator_type"] == "solenoid":
            name = relay["name"]
            relay_type = relay["relay_type"]
            solenoid_type = relay["solenoid_type"]
            default_state = relay["default_state"]

            if relay_type not in ["NO", "NC"]:
                raise ValueError(f"Invalid relay type '{relay_type}' for relay '{name}'.")
            if solenoid_type not in ["NO", "NC"]:
                raise ValueError(f"Invalid solenoid type '{solenoid_type}' for relay '{name}'.")
            if default_state not in ["open", "closed"]:
                raise ValueError(f"Invalid default state '{default_state}' for relay '{name}'.")
            
            power_state = "on" if (default_state == "open" and solenoid_type == "NC") or (default_state == "closed" and solenoid_type == "NO") else "off"
            relay_state = 0 if (power_state == "on" and  relay_type == "NO" ) or (power_state == "off" and relay_type == "NC") else 1
            command = {
                "type": "relay",
                "id": relay["channelID"],
                "state": relay_state
            }
            commands.append(command)
        if relay["actuator_type"] == "poweredDevice":
            name = relay["name"]
            relay_type = relay["relay_type"]
            default_state = relay["default_state"]

            if relay_type not in ["NO", "NC"]:
                raise ValueError(f"Invalid relay type '{relay_type}' for relay '{name}'.")
            if default_state not in ["on", "off"]:
                raise ValueError(f"Invalid default state '{default_state}' for relay '{name}'.")

            relay_state = 0 if (default_state == "on" and  relay_type == "NO" ) or (default_state == "off" and relay_type == "NC") else 1
            command = {
                "type": "relay",
                "id": relay["channelID"],
                "state": relay_state
            }
            commands.append(command)
    
    for command in commands:
        if command == wait:
            time.sleep(2)
        else:
            mqtt_client.publish(mqtt.COMMAND_TOPIC, json.dumps(command))
            time.sleep(0.1)  # Add a small delay between commands to avoid flooding the broker