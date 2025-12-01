import time
import yaml
import random
from datetime import datetime
from data_file import data_store
import config_parser
import data_interface
import command_interface

fake_processed_data = {}

def generate_data():
    global fake_processed_data
    #return {"dummmy":"data"}
    #new_data = {"timestamp": datetime.now().strftime("%H:%M:%S,%d-%m-%Y"), "sensors": [], "actuators":[]}
    raw_data = {"sensors": []}
    processed_data = {"sensors": []}
    for i in range(8):
        raw_data["sensors"].append({
            "hat_id": 0,
            "channel_id": i,
            "value": round(random.uniform(0.0, 100.0), 3),
            "timestamp": int(time.time()) 
        })

    # Process sensors
    for sensor in raw_data.get("sensors", []):
        hat_id = sensor.get("hat_id")
        channel_id = sensor.get("channel_id")
        value = sensor.get("value")
        timestamp = sensor.get("timestamp")

        # Find the sensor in the config using hat_id and channel_id
        sensor_info = next(
            (s for s in config_parser.get_config()["sensors"].values() if s["hatID"] == hat_id and s["channelID"] == channel_id),
            None
        )
        if sensor_info:
            calibration = sensor_info.get("calibration", [])

            # Apply interpolation only if calibration is non-empty
            if calibration and len(calibration) > 0:
                value = data_interface.interpolate(value, calibration)
            
            processed_data["sensors"].append({
                "name": sensor_info["name"],
                "value": f"{value:.2f}",
                "timestamp": timestamp
            })
    if data_interface.SAVE_DATA_FLAG:
        data_interface.save_data(processed_data)  # Save the processed data to a file
    fake_processed_data = processed_data
    return processed_data
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
   # return fake_processed_data

async def handle_dummy_command(command):
    print("dummy command")
    # Handle actuator toggling
    """if command.get("action") == "toggle":
        for actuator in config_data["actuators"]:
            if actuator["name"] == command["name"]:
                actuator["status"] = "on" if actuator["status"] == "off" else "off"
                break"""