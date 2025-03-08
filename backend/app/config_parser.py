import yaml
import numpy as np


CONFIG_FILE = 'config.yml' 
config_data = None

def load_config(config_yml):
    """Load the YAML configuration file."""
    global config_data
    with open(config_yml, 'r') as file:
        config = yaml.safe_load(file)
    
    sensor_map = {sensor['channelId']: sensor for sensor in config.get('MCCDAQ', [])}
    relay_map = {relay['channelId']: relay for relay in config.get('relayBoard', [])}
    servo_map = {servo['channelId']: servo for servo in config.get('PCA9685', [])}
    
    config_data = {
        "sensors": sensor_map,
        "relays": relay_map,
        "servos": servo_map
    }

def update_config(config_yml,new_config):
    with open(config_yml,'w') as file:
        yaml.dump(new_config,file)

def get_config():
    if config_data:
        return config_data
    else:
        load_config(CONFIG_FILE)
        return config_data

def interpolate(value, calibration_points):
    """Perform linear interpolation for sensor calibration."""
    calibration_points = np.array(calibration_points)
    voltages, readings = calibration_points[:, 0], calibration_points[:, 1]
    return np.interp(value, voltages, readings)

async def process_data(raw_data):
    """Process incoming raw sensor and actuator data."""
    processed_data = {"sensors": [], "actuators": []}
    # Process sensors
    for sensor in raw_data.get("sensors", []):
        sensor_info = get_config()["sensors"].get(sensor["id"])
        if sensor_info:
            value = sensor["value"]
            if "calibration" in sensor_info:
                value = interpolate(value, sensor_info["calibration"])
            processed_data["sensors"].append({
                "name": sensor_info["name"],
                "value": f"{value:.2f}",
                "timestamp": sensor["timestamp"]
            })
    
    # Process relays
    for relay in raw_data.get("relays", []):
        relay_info = get_config()["relays"].get(relay["id"])
        if relay_info:
            state = "on" if relay["state"] else "off"
            processed_data["actuators"].append({
                "name": relay_info["name"],
                "state": state
            })
    
    # Process servos
    for servo in raw_data.get("servos", []):
        servo_info = get_config()["servos"].get(servo["id"])
        if servo_info:
            state = "open" if servo["state"] == servo_info["open_angle"] else "close"
            processed_data["actuators"].append({
                "name": servo_info["name"],
                "state": state
            })
    
    return processed_data