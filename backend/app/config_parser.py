import yaml
import numpy as np
import random
from datetime import datetime

CONFIG_FILE = 'configs/config.yml'
config_data = None


def load_config():
    """Load the YAML configuration file."""
    global config_data
    with open(CONFIG_FILE, 'r') as file:
        config = yaml.safe_load(file)
    
    try:
        validate_config(config)
    except ValueError as e:
        print(f"Configuration error: {e}")
        return
    
    sensor_map = {(sensor['channelID'] + 8*sensor['hatID']): sensor for sensor in config.get('MCCDAQ', [])}
    relay_map = {relay['channelID']: relay for relay in config.get('relayBoard', [])}
    servo_map = {servo['channelID']: servo for servo in config.get('PCA9685', [])}
    gpio_map = {gpio['pinID']: gpio for gpio in config.get('GPIOs', [])}
    
    config_data = {
        "sensors": sensor_map,
        "relays": relay_map,
        "servos": servo_map,
        "gpios": gpio_map,
    }
    # Add slope and intercept for each sensor
    for sensor in config_data["sensors"].values():
        calibration = sensor.get("calibration", [])
        degree = sensor.get("degree", 1)
        if calibration and len(calibration) > 0:
            #voltages, readings = zip(*calibration)
            calibration_points = np.array(calibration)
            voltages, readings = calibration_points[:, 0], calibration_points[:, 1]
            m, b = np.polyfit(voltages, readings, degree)
            
            sensor["slope"] = m
            sensor["intercept"] = b
        else:
            sensor["slope"] = 1.0
            sensor["intercept"] = 0.0
    #print("Loaded config:", config_data)

def validate_config(config):
    """Validate the configuration for missing keys."""
    if 'MCCDAQ' not in config:
        raise ValueError("Missing 'MCCDAQ' section in the configuration.")
    if 'relayBoard' not in config:
        raise ValueError("Missing 'relayBoard' section in the configuration.")
    if 'PCA9685' not in config:
        raise ValueError("Missing 'PCA9685' section in the configuration.")

    for sensor in config.get('MCCDAQ', []):
        if 'channelID' not in sensor:
            raise ValueError("Missing 'channelID' in sensor entry: {sensor}")
        if 'hatID' not in sensor:
            raise ValueError("Missing 'hatID' in sensor entry: {sensor}")
        if 'name' not in sensor:
            raise ValueError("Missing 'name' in sensor entry: {sensor}")
        if 'unit' not in sensor:
            print(f"Warning: Missing 'unit' in sensor entry: {sensor}")
        if 'calibration' not in sensor:
            print(f"Warning: Missing 'calibration' in sensor entry: {sensor}")
        if 'type' not in sensor:
            print(f"Warning: Missing 'type' in sensor entry: {sensor}")

    for relay in config.get('relayBoard', []):
        if 'channelID' not in relay:
            raise ValueError("Missing 'channelID' in relay entry: {relay}")
        if 'name' not in relay:
            raise ValueError("Missing 'name' in relay entry: {relay}")
        #if 'type' not in relay:
            #print(f"Warning: Missing 'type' in relay entry: {relay}")
    
    for servo in config.get('PCA9685', []):
        if 'channelID' not in servo:
            raise ValueError("Missing 'channelID' in servo entry: {servo}")
        if 'name' not in servo:
            raise ValueError("Missing 'name' in servo entry: {servo}")
        if 'open_pos' not in servo:
            raise ValueError("Missing 'open_pos' in servo entry: {servo}")
        if 'close_pos' not in servo:
            raise ValueError("Missing 'close_pos' in servo entry: {servo}")
        #if 'open_over' not in servo:
            #print(f"Warning: Missing 'open_over' in servo entry: {servo}")
        #if 'close_over' not in servo:
            #print(f"Warning: Missing 'close_over' in servo entry: {servo}")
    

def update_config(config_filename, new_config):
    global CONFIG_FILE
    with open(f"configs/{config_filename}",'w') as file:
        yaml.dump(new_config, file)
    CONFIG_FILE = f"configs/{config_filename}"
    load_config()  # Reload the configuration after updating

def get_config():
    if config_data:
        return config_data
    else:
        load_config()
        return config_data

def get_actuators_config():
    config = get_config()
    actuators = []
    for relay in config["relays"].values():
        if relay.get("actuator_type") is None:
            # Old format, check the type
            if relay.get("type") == "NC":
                actuator = {"type": "solenoid", "name": relay["name"]}
            else:
                continue  # Skip relays that do not have an actuator type defined
        elif relay.get("actuator_type") == "servo":
            #actuator = {"type": "servo", "name": relay["name"]}
            continue  # Skip relays that are servos, as they are handled separately
        elif relay.get("actuator_type") == "solenoid":
            actuator = {"type": "solenoid", "name": relay["name"]}
        elif relay.get("actuator_type") == "poweredDevice":
            actuator = {"type": "poweredDevice", "name": relay["name"]}
        else:
            continue  # Skip any relays that do not match the expected types
        actuators.append(actuator)
    for servo in config["servos"].values():
        if "open_pos" in servo or "close_pos" in servo:
            actuator = {"type": "servo", "name": servo["name"]}
            actuators.append(actuator)
        elif "pos_1" in servo:
            actuator = {"type": "servo3", "name": servo["name"]}
            actuators.append(actuator)
    for gpio in config["gpios"].values():
        if gpio.get("mode") == "output":
            if "relayID" in gpio:
                actuator = {"type": "poweredGpioDevice", "name": gpio["name"]} # "state": gpio["state"], 
            else:
                actuator = {"type": "gpioDevice", "name": gpio["name"]}
        else:
            continue  # Skip GPIOs that are not outputs
        actuators.append(actuator)
    return actuators 

def get_sensors_config():
    config = get_config()
    sensors = []
    for s in config["sensors"].values():
        sensor = {
            "name": s["name"], 
            "unit": s["unit"],
            "type": s["type"]
            }
        sensors.append(sensor)
    return 