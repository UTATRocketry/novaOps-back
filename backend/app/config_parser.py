import yaml
import numpy as np
import random
from datetime import datetime

CONFIG_FILE = 'configs/config.yml' 
DATA_FILE = None
SAVE_DATA_FLAG = False
CALIBRATION_FLAG = True
config_data = None
test_start = datetime.now()
file_length = 0

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

def save_data(data):
    """Save sensor data to a CSV file."""
    if DATA_FILE is not None:
        with open(f"logs/{DATA_FILE}", 'a') as file:
            # write a global timestamp to the file
            now = datetime.now()
            timedelta = now-test_start
            timestamp = str(timedelta)
            file.write(f"{timestamp},")

            for sensor in get_config()["sensors"].values():
                sensor_name = sensor["name"]
                # find the sensor in the data
                sensor_data = next((s for s in data["sensors"] if s["name"] == sensor_name), None)
                if sensor_data:
                    file.write(f"{sensor_data['value']},")
                else:
                    file.write("N/A,")
            """"
            for gpio in get_config()["gpios"].values():
                gpio_name = gpio["name"]
                # find the gpio in the data
                gpio_data = next((g for g in data["gpios"] if g["name"] == gpio_name), None)
                if gpio_data:
                    file.write(f"{gpio_data['state']},")
                else:
                    file.write("N/A,")
            for actuator in data.get("actuators", []):
                actuator_name = actuator["name"]
                # find the actuator in the config
                actuator_info = next((a for a in get_config()["relays"].values() if a["name"] == actuator_name), None)
                if actuator_info:
                    file.write(f"{actuator['state']},")
                else:
                    file.write("N/A,")
            for thermocouple in data.get("thermocouples", []):
                thermocouple_name = thermocouple["name"]
                # find the thermocouple in the config
                thermocouple_info = next((t for t in get_config()["thermocouples"].values() if t["name"] == thermocouple_name), None)
                if thermocouple_info:
                    file.write(f"{thermocouple['value']},")
                else:
                    file.write("N/A,")
            """
            file.write("\n")
    
    
def interpolate(value, calibration_points, degree=1):
    """Perform linear interpolation for sensor calibration."""
    calibration_points = np.array(calibration_points)
    voltages, readings = calibration_points[:, 0], calibration_points[:, 1]
    m, b = np.polyfit(voltages, readings, degree)
    return m*value + b

async def process_data(raw_data, data_type):
    """Process incoming raw sensor and actuator data."""
    processed_data = []
    
    # Process sensors
    if data_type == "sensors":
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

                # generate fake data for testing
                value = round(random.uniform(-5.0, 10.0), 3)
                
                # Apply interpolation only if calibration is non-empty
                if calibration and len(calibration) > 0 and CALIBRATION_FLAG:
                    value = sensor_info["slope"] * value + sensor_info["intercept"]
                
                processed_data.append({
                    "name": sensor_info["name"],
                    "value": f"{round(value, 2)}",
                    "unit": sensor_info.get("unit", ""),
                    "timestamp": timestamp
                })

    # Process GPIOs
    if data_type == "gpios":
        for gpio in raw_data.get("gpios", []):
            pin_id = gpio.get("pin_id")
            state = gpio.get("state")
            timestamp = gpio.get("timestamp")

            # Find the GPIO in the config using pin_id
            gpio_info = next(
                (g for g in get_config()["gpios"].values() if g["pinID"] == pin_id),
                None
            )
            if gpio_info:
                processed_data.append({
                    "name": gpio_info["name"],
                    "state": state
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
    return processed_data


async def validate_command(command):
    if not isinstance(command, dict):
        raise ValueError("Command must be a dictionary.")
    if "type" not in command or "name" not in command or "state" not in command:
        raise ValueError("Command must contain 'type', 'name', and 'state' keys.")
    #if command["type"] not in ["solenoid", "servo", "poweredDevice", "gpioDevice", "poweredGpioDevice"]:
       # raise ValueError("Command type must be either 'solenoid' or 'servo'.")
    if command["type"] == "solenoid":
        relay = next((r for r in get_config()["relays"].values() if r["name"] == command["name"]), None)
        if not relay:
            raise ValueError(f"Relay with name '{command['name']}' not found in config.")
        if command["state"] not in ["open", "closed"]:
            raise ValueError("Solenoid state must be either 'open' or 'closed'.")
        if "type" not in relay and "relay_type" not in relay:
            raise ValueError(f"Relay '{command['name']}' must have 'type' or 'relay_type' defined.")
    if command["type"] == "servo":
        config = get_config()
        servo = config["servos"].get(command["name"], None)
        if not servo:
            raise ValueError(f"Servo with name '{command['name']}' not found in config.")
        if command["state"] not in ["open", "closed", "on", "off"]:
            raise ValueError("Servo state must be either 'open', 'closed', 'on', or 'off'.")
        if command["state"] in ["on", "off"] and "relayID" not in servo:
            raise ValueError(f"Servo '{command['name']}' does not have a relayID for 'on'/'off' state.")
        if command["state"] in ["open", "closed"] and ("open_pos" not in servo or "close_pos" not in servo):
            raise ValueError(f"Servo '{command['name']}' must have 'open_pos' and 'close_pos' for 'open'/'closed' state.")
    return True


async def convert_command(command):
    """
    Convert and send command values based on config.yml values.
    """
    config = get_config()  # Load the configuration
    command_type = command["type"]
    name = command["name"]
    state = command["state"]
    
    if command_type in ["poweredDevice"] and state in ["on", "off"]:
        # Find the relay by name
        relay = next((r for r in config["relays"].values() if r["name"] == name), None)
        if not relay:
            print(f"Relay with name '{name}' not found in config.")
            raise ValueError(f"Relay with name '{name}' not found in config.")
        
        relay_state = 0 if (state == "on" and  relay["relay_type"] == "NO" ) or (state == "off" and relay["relay_type"] == "NC") else 1
        
        # Create the command
        relay_command = {
            "type": "relay",
            "id": relay["channelID"],
            "state": relay_state
        }
        return [relay_command]
    
    elif command_type in ["gpioDevice", "poweredGpioDevice"]:
        # Find the GPIO by name
        gpio = next((g for g in config["gpios"].values() if g["name"] == name), None)
        if not gpio:
            raise ValueError(f"GPIO with name '{name}' not found in config.")
        
        if state in ["on", "off"]:
            relay_state = 0 if (state == "on") else 1
            if "relayID" in gpio:
                relay_command = {
                    "type": "relay",
                    "id": gpio["relayID"],
                    "state": relay_state
                }
                return [relay_command]

        if state  in ["armed", "disarmed"]:
            # Convert state to GPIO state
            gpio_state = 1 if state == "armed" else 0
            
            # Create the command
            gpio_command = {
                "type": "gpio",
                "id": gpio["pinID"],
                "mode": "output",
                "state": gpio_state
            }
            return [gpio_command]
    
    elif command_type == "solenoid":
        # Find the relay by name
        relay = next((r for r in config["relays"].values() if r["name"] == name), None)
        if not relay:
            print(f"Relay with name '{name}' not found in config.")
            raise ValueError(f"Relay with name '{name}' not found in config.")

        relay_type = relay.get("relay_type", None)
        if relay_type is None:
            print(f"Relay '{name}' does not have a 'relay_type' defined.")
            raise ValueError(f"Relay '{name}' does not have a 'relay_type' defined.")
        elif relay_type not in ["NO", "NC"]:
            print(f"Invalid relay type '{relay_type}' for relay '{name}'.")
            raise ValueError(f"Invalid relay type '{relay_type}' for relay '{name}'.")
        
        solenoid_type = relay["solenoid_type"]
        if solenoid_type is None:
            print(f"Relay '{name}' does not have a 'solenoid_type' defined.")
            raise ValueError(f"Relay '{name}' does not have a 'solenoid_type' defined.")
        elif solenoid_type not in ["NO", "NC"]:
            print(f"Invalid solenoid type '{solenoid_type}' for relay '{name}'.")
            raise ValueError(f"Invalid solenoid type '{solenoid_type}' for relay '{name}'.")
        
        # Convert state based on relay type
        relay_state = None
        if state == "open":
            # Convert state based on relay type
            if solenoid_type == "NO":
                relay_state = 0 if relay_type == "NO" else 1
            elif solenoid_type == "NC":
                relay_state = 1 if relay_type == "NO" else 0
        elif state == "closed":
            if solenoid_type == "NO":
                relay_state = 1 if relay_type == "NO" else 0
            elif solenoid_type == "NC":
                relay_state = 0 if relay_type == "NO" else 1
        
        # power_state = "on" if (state == "open" and solenoid_type == "NC") or (state == "closed" and solenoid_type == "NO") else "off"
        # relay_state = 0 if (power_state == "on" and  relay_type == "NO" ) or (power_state == "off" and relay_type == "NC") else 1
        
        if relay_state is None:
            print(f"Invalid state '{state}' for relay '{name}'.")
            raise ValueError(f"Invalid state '{state}' for relay '{name}'.")
        
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
        elif state in ["on", "off"]:
            relay_state = 0 if (state == "on") else 1
            if "relayID" in servo:
                relay_command = {
                    "type": "relay",
                    "id": servo["relayID"],
                    "state": relay_state
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
    

    