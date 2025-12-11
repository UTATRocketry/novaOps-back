import time
import yaml
import random
import asyncio
from datetime import datetime
from data_file import data_store
import config_parser
import data_interface

fake_processed_data = {}
raw_data = {}  # Make this global

value = 50.0  # initial

def next_value(value, step=5.0):
    change = random.uniform(-step, step)
    new = value + change
    return max(0.0, min(100.0, new))  # clamp

def generate_data():
    global fake_processed_data, raw_data
    raw_data = {"sensors": []}
    processed_data = {"sensors": []}
    for i in range(8):
        raw_data["sensors"].append({
            "hat_id": 0,
            "channel_id": i,
            "value": next_value(value),
            "timestamp": int(time.time() * 1000)
        })

#     # Process sensors
#     for sensor in raw_data.get("sensors", []):
#         hat_id = sensor.get("hat_id")
#         channel_id = sensor.get("channel_id")
#         value = sensor.get("value")
#         timestamp = sensor.get("timestamp")

#         # Find the sensor in the config using hat_id and channel_id
#         sensor_info = next(
#             (s for s in config_parser.get_config()["sensors"].values() if s["hatID"] == hat_id and s["channelID"] == channel_id),
#             None
#         )
#         if sensor_info:
#             calibration = sensor_info.get("calibration", [])

#             # Apply interpolation only if calibration is non-empty
#             if calibration and len(calibration) > 0:
#                 value = data_interface.interpolate(value, calibration)
            
#             processed_data["sensors"].append({
#                 "name": sensor_info["name"],
#                 "value": f"{value:.2f}",
#                 "timestamp": timestamp
#             })
#     if data_interface.SAVE_DATA_FLAG:
#         data_interface.save_data(processed_data)  # Save the processed data to a file
#     fake_processed_data = processed_data
#     return processed_data
    """
    for actuator in get_config()["actuators"]:
        new_data["actuators"].append({
            "actuator_id": actuator.get("actuator_id"),
            #"actuator_name": actuator.get("actuator_name"),
            #"actuator_type": actuator.get("actuator_type"),
            "actuator_status": "off",
            "timestamp": int(time.time())
        })
    """

async def handle_dummy_command(command):
    print("dummy command")
    # Handle actuator toggling
    """if command.get("action") == "toggle":
        for actuator in config_data["actuators"]:
            if actuator["name"] == command["name"]:
                actuator["status"] = "on" if actuator["status"] == "off" else "off"
                break"""

async def publish_command(command):
    """Replacement for mqtt.mqtt_client.publish()"""
    print(f"Publishing command (dummy mode): {command}")
    await handle_dummy_command(command)
    return {"status": "Command sent (dummy mode)"}

async def set_to_defaults():
    """Replacement for mqtt.set_to_defaults()"""
    # This should call command_interface.set_to_defaults() which will use our publish_command
    return await command_interface.set_to_defaults()

async def start_dummy_data():
    """Background task to continuously generate data"""
    while True:
        generate_data()
        # Process raw_data through data_interface like MQTT does
        # Raw data gets updated every 0.001 seconds
        if raw_data:
            # Therefore processed_data gets updated every ~0.001 seconds
            await data_interface.process_data(raw_data)
        await asyncio.sleep(0.001)
