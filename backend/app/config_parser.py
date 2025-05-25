import yaml
import numpy as np
from datetime import datetime

CONFIG_FILE = 'configs/config.yml' 
DATA_FILE = None
SAVE_DATA_FLAG = False
config_data = None

def load_config(config_yml=CONFIG_FILE):
    """Load the YAML configuration file."""
    global config_data
    with open(config_yml, 'r') as file:
        config = yaml.safe_load(file)
    
    validate_config(config)

    sensor_map = {(sensor['channelID'] + 8*sensor['hatID']): sensor for sensor in config.get('MCCDAQ', [])}
    relay_map = {relay['channelID']: relay for relay in config.get('relayBoard', [])}
    servo_map = {servo['channelID']: servo for servo in config.get('PCA9685', [])}
    
    config_data = {
        "sensors": sensor_map,
        "relays": relay_map,
        "servos": servo_map
    }
    #print("Loaded config:", config_data)

def validate_config(config):
    """Validate the configuration for missing keys."""
    for relay in config.get('relayBoard', []):
        if 'channelID' not in relay:
            print(f"Warning: Missing 'channelID' in relay entry: {relay}")
    for sensor in config.get('MCCDAQ', []):
        if 'channelID' not in sensor:
            print(f"Warning: Missing 'channelID' in sensor entry: {sensor}")
    for servo in config.get('PCA9685', []):
        if 'channelID' not in servo:
            print(f"Warning: Missing 'channelID' in servo entry: {servo}")

def update_config(config_yml,new_config):
    with open(config_yml,'w') as file:
        yaml.dump(new_config,file)

def get_config():
    if config_data:
        return config_data
    else:
        load_config()
        return config_data


def save_data(data):
    """Save sensor data to a CSV file."""
    if DATA_FILE is not None:
        with open(f"logs/{DATA_FILE}", 'a') as file:
            # write a global timestamp to the file
            now = datetime.now()
            timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
            file.write(f"{timestamp},")

            for sensor in get_config()["sensors"].values():
                sensor_name = sensor["name"]
                # find the sensor in the data
                sensor_data = next((s for s in data["sensors"] if s["name"] == sensor_name), None)
                if sensor_data:
                    file.write(f"{sensor_data['value']},")
                else:
                    file.write("N/A,")
            file.write("\n")
    
    
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
        hat_id = sensor.get("hat_id")
        channel_id = sensor.get("channel_id")
        value = sensor.get("value")
        timestamp = sensor.get("timestamp")

        # Find the sensor in the config using hat_id and channel_id
        sensor_info = next(
            (s for s in get_config()["sensors"].values() if s["hatID"] == hat_id and s["channelID"] == channel_id),
            None
        )
        if sensor_info:
            calibration = sensor_info.get("calibration", [])

            # Apply interpolation only if calibration is non-empty
            if calibration and len(calibration) > 0:
                value = interpolate(value, calibration)
            
            processed_data["sensors"].append({
                "name": sensor_info["name"],
                "value": f"{round(value, 2)}",
                #"unit": sensor_info.get("unit", ""),
                "timestamp": timestamp
            })
    """
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
    """
    if SAVE_DATA_FLAG:
        save_data(processed_data)  # Save the processed data to a file
    return processed_data


    
async def convert_command(command):
    """
    Convert and send command values based on config.yml values.
    """
    config = get_config()  # Load the configuration
    command_type = command["type"]
    name = command["name"]
    state = command["state"]
    

    if command_type == "solenoid":
        # Find the relay by name
        relay = next((r for r in config["relays"].values() if r["name"] == name), None)
        if not relay:
            raise ValueError(f"Relay with name '{name}' not found in config.")

        if (relay["type"]):
            # Convert state based on relay type
            relay_type = relay["type"]
            relay_state = 1 if (state == "open" and relay_type == "NO") or (state == "closed" and relay_type == "NC") else 0
        else:
            relay_state = 1 if (state == "open") else 0
        
        # Create the command
        relay_command = {
            "type": "relay",
            "id": relay["channelID"],
            "state": relay_state
        }

        return [relay_command]
    elif command_type == "servo":
        mqtt_commands = []
        relay_state = None
        angle = None
        # Find the servo by name
        servo = next((s for s in config["servos"].values() if s["name"] == name), None)
        if not servo:
            raise ValueError(f"Servo with name '{name}' not found in config.")

        # Determine the angle based on state
        if state == "open":
            angle = servo["open_pos"]
            over_angle = servo.get("open_over")
        elif state == "closed":
            angle = servo["close_pos"]
            over_angle = servo.get("close_over")

            
        elif state == "on" or state == "off":
            relay_state = 0 if (state == "on") else 1
            # If relayId exists, send a command to turn the relay on
            if "relayID" in servo:
                relay_command = {
                    "type": "relay",
                    "id": servo["relayID"],
                    "state": relay_state  # Turn relay on
                }
                return [relay_command]
        else:
            raise ValueError(f"Invalid state '{state}' for servo.")


        # If over position exists, send the over position first
        if over_angle:
            over_command = {
                "type": "servo",
                "id": servo["channelID"],
                "angle": over_angle
            }
            mqtt_commands.append(over_command)

        if angle:
            # Send the actual position command
            servo_command = {
                "type": "servo",
                "id": servo["channelID"],
                "angle": angle
            }
            mqtt_commands.append(servo_command)

        return mqtt_commands
    else:
        raise ValueError(f"Invalid command type '{command_type}'.")
    

    