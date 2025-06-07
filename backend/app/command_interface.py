import time
import config_parser
import mqtt_interface

actuator_states = {}

async def validate_command(command):
    if not isinstance(command, dict):
        raise ValueError("Command must be a dictionary.")
    if "type" not in command or "name" not in command or "state" not in command:
        raise ValueError("Command must contain 'type', 'name', and 'state' keys.")
    #if command["type"] not in ["solenoid", "servo", "poweredDevice", "gpioDevice", "poweredGpioDevice"]:
       # raise ValueError("Command type must be either 'solenoid' or 'servo'.")
    if command["type"] == "solenoid":
        relay = next((r for r in config_parser.get_config()["relays"].values() if r["name"] == command["name"]), None)
        if not relay:
            raise ValueError(f"Relay with name '{command['name']}' not found in config.")
        if command["state"] not in ["open", "closed"]:
            raise ValueError("Solenoid state must be either 'open' or 'closed'.")
        if "type" not in relay and "relay_type" not in relay:
            raise ValueError(f"Relay '{command['name']}' must have 'type' or 'relay_type' defined.")
    if command["type"] == "servo":
        config = config_parser.get_config()
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
    config = config_parser.get_config()  # Load the configuration
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

    
async def set_all_to_closed():
    """Set all servos and solenoids to their closed state."""
    config = config_parser.get_config()
    commands = []
    wait = {"wait"}
    for servo in config["servos"].values():
        name = servo["name"]
        command = {
        "type": "servo",
        "id": servo["channelID"],
        "angle": servo["close_pos"]
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
            commands.append(power_on)
            commands.append(wait)
            commands.append(command)
            commands.append(wait)
            commands.append(power_off)
        else:
            commands.append(command)
    for relay in config["relays"].values():
        # Send commands to set all solenoids to their default state
        if relay["actuator_type"] == "solenoid":
            name = relay["name"]
            relay_type = relay["relay_type"]
            solenoid_type = relay["solenoid_type"]

            if relay_type not in ["NO", "NC"]:
                raise ValueError(f"Invalid relay type '{relay_type}' for relay '{name}'.")
            if solenoid_type not in ["NO", "NC"]:
                raise ValueError(f"Invalid solenoid type '{solenoid_type}' for relay '{name}'.")
            
            relay_state = 0 if (solenoid_type == "NO" and  relay_type == "NO" ) or (solenoid_type == "NC" and relay_type == "NC") else 1
            command = {
                "type": "relay",
                "id": relay["channelID"],
                "state": relay_state
            }
            commands.append(command)
        if relay["actuator_type"] == "poweredDevice":
            name = relay["name"]
            relay_type = relay["relay_type"]

            if relay_type not in ["NO", "NC"]:
                raise ValueError(f"Invalid relay type '{relay_type}' for relay '{name}'.")

            relay_state = 0 if (relay_type == "NC") else 1
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
            mqtt_interface.publish_command(command)
            time.sleep(0.1)  # Add a small delay between commands to avoid flooding the broker
    return {"status": "Commands sent"}

async def set_to_defaults():
    """Set all actuators to their default state."""
    config = config_parser.get_config()
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
            mqtt_interface.publish_command(command)
            time.sleep(0.1)  # Add a small delay between commands to avoid flooding the broker