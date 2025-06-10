import numpy as np
import random
from datetime import datetime
import config_parser

DATA_FILE = None
SAVE_DATA_FLAG = False
CALIBRATION_FLAG = True
ROLLING_WINDOW_SIZE = 400  # number of samples to use for rolling average
RATE_WINDOW_SIZE = 200
test_start = datetime.now()
file_num = 0
file_length = 0
processed_data = {"sensors": []} #"gpios": []
data_store = {}

def new_data_file():
    global file_num, DATA_FILE
    date = datetime.now().strftime("%Y-%m-%d-%H")
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
    if DATA_FILE is not None:
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
    else:
        print("Error saving data: no data file")
        raise ValueError("Error saving data: no data file")
    
    
def interpolate(value, calibration_points, degree=1):
    """Perform linear interpolation for sensor calibration."""
    calibration_points = np.array(calibration_points)
    voltages, readings = calibration_points[:, 0], calibration_points[:, 1]
    m, b = np.polyfit(voltages, readings, degree)
    return m*value + b

async def process_data(raw_data):
    """Process incoming raw sensor and actuator data."""
    global processed_data, data_store
    sensor_data = []
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
                name = sensor_info["name"]
                calibration = sensor_info.get("calibration", [])

                # generate fake data for testing
                # value = round(random.uniform(-5.0, 10.0), 3)
                
                # Apply interpolation only if calibration is non-empty
                if calibration and len(calibration) > 0 and CALIBRATION_FLAG:
                    value = sensor_info["slope"] * value + sensor_info["intercept"]
                
                # Initialize history for this sensor
                if name not in data_store:
                    data_store[name] = []

                # Append new value with timestamp
                data_store[name].append((timestamp, value))
                if len(data_store[name]) > ROLLING_WINDOW_SIZE:
                    data_store[name].pop(0)

                # Compute rolling average
                rolling_values = [v for t, v in data_store[name]]
                avg_value = np.mean(rolling_values)

                # Compute rate of change
                if len(data_store[name]) >= RATE_WINDOW_SIZE:
                    t0, v0 = data_store[name][0]
                    t1, v1 = data_store[name][RATE_WINDOW_SIZE - 1]
                    # Convert timestamps to datetime if needed
                    #if isinstance(t0, str):
                    #    t0 = datetime.fromisoformat(t0)
                    #if isinstance(t1, str):
                    #    t1 = datetime.fromisoformat(t1)
                elif len(data_store[name]) >= 2:
                    t0, v0 = data_store[name][0]
                    t1, v1 = data_store[name][-1]
                else:
                    t1, t0 = 0
                # Now compute the rate
                if t1 != t0:
                    dt = t1 - t0  # time difference in seconds
                    rate = (v1 - v0) / dt
                    # If the time span is less than 1 second, normalize to per second
                    if dt < 1.0:
                        rate *= (1.0 / dt)
                    rate_str = f"{round(rate, 2)}"
                else:
                    rate_str = "N/A"

                sensor_data.append({
                    "name": sensor_info["name"],
                    "value": f"{round(value, 2)}",
                    "avg": f"{round(avg_value, 2)}",
                    "rate": rate_str,
                    "unit": sensor_info.get("unit", ""),
                    "timestamp": timestamp,
                })
    processed_data["sensors"] = sensor_data
    if SAVE_DATA_FLAG:
        save_data(processed_data["sensors"])  # Save the processed data to a file