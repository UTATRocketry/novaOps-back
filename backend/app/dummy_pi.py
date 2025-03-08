import time
import yaml
import random
from datetime import datetime
from data_file import data_store
from config_parser import get_config 


def generate_data():
    return {"dummmy":"data"}
    """new_data = {"timestamp": datetime.now().strftime("%H:%M:%S,%d-%m-%Y"), "sensors": [], "actuators":[]}
    for sensor in get_config()["sensors"]:
        new_data["sensors"].append({
            "sensor_id": sensor.get("sensor_id"),
            #"name": sensor.get("sensor_name"),
            #"type": sensor.get("sensor_type"),
            "sensor_value": round(random.uniform(0.0, 100.0), 2),
            "timestamp": int(time.time()) 
        })
    for actuator in get_config()["actuators"]:
        new_data["actuators"].append({
            "actuator_id": actuator.get("actuator_id"),
            #"actuator_name": actuator.get("actuator_name"),
            #"actuator_type": actuator.get("actuator_type"),
            "actuator_status": "off",
            "timestamp": int(time.time())
        })
    return new_data"""

async def handle_dummy_command(command):
    print("dummy command")
    # Handle actuator toggling
    """if command.get("action") == "toggle":
        for actuator in config_data["actuators"]:
            if actuator["name"] == command["name"]:
                actuator["status"] = "on" if actuator["status"] == "off" else "off"
                break"""