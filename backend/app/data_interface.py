import numpy as np
import random
from datetime import datetime
import config_parser

DATA_FILE = None
SAVE_DATA_FLAG = False
CALIBRATION_FLAG = True
test_start = datetime.now()
file_num = 0
file_length = 0
processed_data = {"sensors": []} #"gpios": []
data_store = {}

def new_data_file():
    date = datetime.now().strftime("%Y-%m-%d")
    DATA_FILE = f"{date}_data_{file_num}.csv"
    # write the header to the file: Timestamp then each sensor name in the config
    with open(f"logs/{DATA_FILE}", 'w') as file:
        file.write("Timestamp,")
        for sensor in config_parser.get_config()["sensors"].values():
            file.write(f"{sensor['name']},")
        file.write("\n")
    file_num += 1
    return

def save_data(data):
    """Save sensor data to a CSV file."""
    if DATA_FILE is None:
        new_data_file()
        
    with open(f"logs/{DATA_FILE}", 'a') as file:
        # write a global timestamp to the file
        now = datetime.now()
        timedelta = now-test_start
        timestamp = str(timedelta)
        file.write(f"{timestamp},")

        for sensor in config_parser.get_config()["sensors"].values():
            sensor_name = sensor["name"]
            # find the sensor in the data
            sensor_data = next((s for s in data if s["name"] == sensor_name), None)
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

async def process_data(raw_data):
    """Process incoming raw sensor and actuator data."""

    # Process sensors
    if "sensors" in raw_data:
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

                # generate fake data for testing
                # value = round(random.uniform(-5.0, 10.0), 3)
                
                # Apply interpolation only if calibration is non-empty
                if calibration and len(calibration) > 0 and CALIBRATION_FLAG:
                    value = sensor_info["slope"] * value + sensor_info["intercept"]
                
                data_store[sensor_info["name"]]
                processed_data["sensors"].append({
                    "name": sensor_info["name"],
                    "value": f"{round(value, 2)}",
                    "unit": sensor_info.get("unit", ""),
                    "timestamp": timestamp
                })
    """
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
    if SAVE_DATA_FLAG:
        save_data(processed_data["sensors"])  # Save the processed data to a file
    