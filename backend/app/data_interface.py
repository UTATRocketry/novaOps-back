import numpy as np
import random
from datetime import datetime
import config_parser

DATA_FILE = None
SAVE_DATA_FLAG = False
CALIBRATION_FLAG = True
DATA_STORE_SIZE = 300  # maximum number of samples to store in memory
ROLLING_WINDOW_SIZE = 100  # number of samples to use for rolling average
RATE_WINDOW_SIZE = 50  # number of samples to use for rate of change
test_start = datetime.now()
file_num = 0
file_length = 0
processed_data = {"sensors": [], "uart": {}} #"gpios": []
data_store = {}

FMC_VECTOR_FIELDS = {
    "accel": "m/s^2",
    "imu_accel": "m/s^2",
    "imu_gyro": "deg/s",
    "mag": "",
}

FMC_SCALAR_FIELDS = {
    "baro_temp": "C",
    "baro_pressure": "Pa",
    "baro_altitude": "m",
    "gps_latitude": "deg",
    "gps_longitude": "deg",
    "gps_altitude": "m",
    "gps_speed": "m/s",
    "gps_course": "deg",
    "gps_sats": "",
    "gps_fix": "",
    "gps_hour": "",
    "gps_minute": "",
    "gps_second": "",
    "temp_h7": "C",
    "temp_pwr": "C",
}

CAN_NODE_NAMES = {
    2: "FMC",
    3: "PMB",
    4: "EPB1",
    5: "EPB2",
    6: "EPB3",
    7: "EPB4",
}

UART_SENSOR_PREFIXES = ("FMC ", "EPB1 ", "EPB2 ", "EPB3 ", "EPB4 ", "PMB ")

def new_data_file():
    global file_num, DATA_FILE
    date = datetime.now().strftime("%Y-%m-%d-%H")
    DATA_FILE = f"{date}_data_{file_num}.csv"
    # write the header to the file: Timestamp then each sensor name in the config
    #with open(f"logs/{DATA_FILE}", 'w') as file:
    #    file.write("Timestamp,")
    #    for sensor in config_parser.get_config()["sensors"].values():
    #        file.write(f"{sensor['name']},")
    #    file.write("\n")
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
    
def format_for_ws():
    global data_store
    ws_data = processed_data
    for sensor in ws_data.get("sensors", []):
        name = sensor["name"]

        # ----- cast value -----
        try:
            value = float(sensor.get("value"))
        except (TypeError, ValueError):
            # skip this sensor if value is missing or bad
            continue

        # ----- cast timestamp -----
        ts_raw = sensor.get("timestamp")
        if ts_raw is None:
            continue                      # or use datetime.now().timestamp()

        # If your source is µs since epoch as an int/str
        timestamp = float(ts_raw) #* MICROS_TO_SECONDS
        # If it’s already seconds as float, just: timestamp = float(ts_raw)

        # history buffer
        if name not in data_store:
            data_store[name] = []
        
        data_store[name].append((timestamp, value))
        if len(data_store[name]) > DATA_STORE_SIZE:
            data_store[name].pop(0)

        # rolling stats
        avg_value = get_rolling_average(name)
        rate      = get_rolling_rate(name)

        sensor["avg"]  = f"{round(avg_value, 2)}"
        sensor["rate"] = rate

    return ws_data

def get_rolling_average(sensor_name):
    """Calculate the rolling average for a given sensor."""
    # calculate the average of the last ROLLING_WINDOW_SIZE values
    if sensor_name not in data_store:
        return float("nan")
    if len(data_store[sensor_name]) == 0:
        return float("nan")
    if len(data_store[sensor_name]) < ROLLING_WINDOW_SIZE:
        # If not enough data, return the average of all available values
        values = [float(v) for _, v in data_store[sensor_name]]
        return np.mean(values) if values else float("nan")
    if len(data_store[sensor_name]) > ROLLING_WINDOW_SIZE:
        # If more than ROLLING_WINDOW_SIZE, use only the last ROLLING_WINDOW_SIZE values
        values = [float(v) for _, v in data_store[sensor_name][-ROLLING_WINDOW_SIZE:]]
    else:
        # If exactly ROLLING_WINDOW_SIZE, use all values
        values = [float(v) for _, v in data_store[sensor_name]]
    return np.mean(values) if values else float("nan")


def get_rolling_rate(sensor_name):
    hist = data_store[sensor_name]
    n    = len(hist)

    if n >= RATE_WINDOW_SIZE:
        t0, v0 = hist[0]
        t1, v1 = hist[RATE_WINDOW_SIZE - 1]
    elif n >= 2:
        t0, v0 = hist[0]
        t1, v1 = hist[-1]
    else:
        return "N/A"

    dt = t1 - t0
    if dt <= 0:
        return "N/A"

    rate = (v1 - v0) / dt
    if dt < 1.0:               # “Δ per second” normalisation
        rate *= (1.0 / dt)

    return f"{round(rate, 2)}"  # round to 2 decimal places

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
                    t1, t0, v1, v0 = 0, 0, 0, 0

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
    uart_sensors = _current_uart_sensor_rows()
    processed_data["sensors"] = sensor_data + uart_sensors
    if SAVE_DATA_FLAG:
        save_data(processed_data["sensors"])  # Save the processed data to a file

async def process_uart_data(raw_data):
    """Process decoded UART frames published by novaGround."""
    global processed_data, data_store

    frame = raw_data.get("frame", {})
    processed_data["uart"] = frame

    msg_name = frame.get("msg_name")
    decoded = frame.get("decoded", {})

    if msg_name == "ack":
        processed_data["uart_ack"] = decoded
        return

    if msg_name == "err":
        processed_data["uart_error"] = frame
        return

    if msg_name != "telem":
        return

    if frame.get("telem_schema") == "fmc_snapshot_v1":
        _process_fmc_telem(decoded)
        return

    if frame.get("telem_schema") == "can_bridge":
        _process_can_bridge_telem(frame)
        return

def _process_fmc_telem(decoded):
    timestamp = decoded.get("timestamp_ms")
    uart_sensor_data = []

    for field, unit in FMC_VECTOR_FIELDS.items():
        vector = decoded.get(field)
        if not isinstance(vector, dict):
            continue
        for axis in ["x", "y", "z"]:
            name = f"FMC {field} {axis}"
            value = vector.get(axis)
            uart_sensor_data.append(_build_uart_sensor_row(name, value, unit, timestamp))

    for field, unit in FMC_SCALAR_FIELDS.items():
        name = f"FMC {field}"
        value = decoded.get(field)
        uart_sensor_data.append(_build_uart_sensor_row(name, value, unit, timestamp))

    regular_sensors = _current_non_uart_sensor_rows()
    processed_data["sensors"] = regular_sensors + uart_sensor_data

def _process_can_bridge_telem(frame):
    payload = frame.get("payload", [])
    if len(payload) < 3:
        return

    sender = frame.get("sender", payload[0])
    telem_len = frame.get("telem_len", payload[2])
    can_payload = payload[3:3 + telem_len]
    node_name = CAN_NODE_NAMES.get(sender, f"node_{sender}")

    processed_data.setdefault("uart_decoded", {})

    if sender in (4, 5, 6, 7):
        decoded = _decode_epb_telem(can_payload)
        processed_data["uart_decoded"][node_name] = decoded
        _replace_uart_rows_for_prefix(node_name, [
            _build_uart_sensor_row(f"{node_name} pressure_one", decoded.get("pressure_one_kpa"), "kPa", None),
            _build_uart_sensor_row(f"{node_name} pressure_two", decoded.get("pressure_two_kpa"), "kPa", None),
            _build_uart_sensor_row(f"{node_name} act_cmd_mask", decoded.get("act_cmd_mask"), "", None),
            _build_uart_sensor_row(f"{node_name} act_ok_mask", decoded.get("act_ok_mask"), "", None),
            _build_uart_sensor_row(f"{node_name} board_temp", decoded.get("board_temp_cC", 0) / 100.0, "C", None),
        ])
        return

    if sender == 3:
        decoded = _decode_pmb_telem(can_payload)
        processed_data["uart_decoded"][node_name] = decoded
        _replace_uart_rows_for_prefix(node_name, [
            _build_uart_sensor_row(f"{node_name} voltage_batt", decoded.get("voltage_batt"), "V", None),
            _build_uart_sensor_row(f"{node_name} voltage_24v", decoded.get("voltage_24v"), "V", None),
            _build_uart_sensor_row(f"{node_name} voltage_8v4", decoded.get("voltage_8v4"), "V", None),
            _build_uart_sensor_row(f"{node_name} voltage_egse", decoded.get("voltage_egse"), "V", None),
            _build_uart_sensor_row(f"{node_name} tempboost", decoded.get("tempboost_C"), "C", None),
            _build_uart_sensor_row(f"{node_name} tempbuck", decoded.get("tempbuck_C"), "C", None),
            _build_uart_sensor_row(f"{node_name} tempamb", decoded.get("tempamb_C"), "C", None),
            _build_uart_sensor_row(f"{node_name} current_8v4", decoded.get("current_8v4"), "A", None),
            _build_uart_sensor_row(f"{node_name} current_24v", decoded.get("current_24v"), "A", None),
        ])

def _decode_epb_telem(payload):
    if len(payload) < 10:
        return {"decode_error": "short_epb_telem", "raw": payload}
    return {
        "sender": payload[0],
        "pressure_one_kpa": _u16_le(payload, 1),
        "pressure_two_kpa": _u16_le(payload, 3),
        "act_cmd_mask": payload[5],
        "act_ok_mask": _u16_le(payload, 6),
        "board_temp_cC": _u16_le(payload, 8),
    }

def _decode_pmb_telem(payload):
    if len(payload) < 37:
        return {"decode_error": "short_pmb_telem", "raw": payload}
    return {
        "sender": payload[0],
        "voltage_batt": _f32_le(payload, 1),
        "voltage_24v": _f32_le(payload, 5),
        "voltage_8v4": _f32_le(payload, 9),
        "voltage_egse": _f32_le(payload, 13),
        "tempboost_C": _f32_le(payload, 17),
        "tempbuck_C": _f32_le(payload, 21),
        "tempamb_C": _f32_le(payload, 25),
        "current_8v4": _f32_le(payload, 29),
        "current_24v": _f32_le(payload, 33),
    }

def _u16_le(payload, offset):
    return int(payload[offset]) | (int(payload[offset + 1]) << 8)

def _f32_le(payload, offset):
    import struct
    return struct.unpack("<f", bytes(payload[offset:offset + 4]))[0]

def _is_uart_sensor_row(sensor):
    name = sensor.get("name", "")
    return name.startswith(UART_SENSOR_PREFIXES)

def _current_uart_sensor_rows():
    return [
        sensor for sensor in processed_data.get("sensors", [])
        if _is_uart_sensor_row(sensor)
    ]

def _current_non_uart_sensor_rows():
    return [
        sensor for sensor in processed_data.get("sensors", [])
        if not _is_uart_sensor_row(sensor)
    ]

def _replace_uart_rows_for_prefix(prefix, rows):
    existing = [
        sensor for sensor in processed_data.get("sensors", [])
        if not sensor.get("name", "").startswith(f"{prefix} ")
    ]
    processed_data["sensors"] = existing + rows

def _build_uart_sensor_row(name, value, unit, timestamp):
    if value is None:
        value = 0

    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        numeric_value = 0.0

    if name not in data_store:
        data_store[name] = []

    data_store[name].append((timestamp, numeric_value))
    if len(data_store[name]) > ROLLING_WINDOW_SIZE:
        data_store[name].pop(0)

    rolling_values = [v for _, v in data_store[name]]
    avg_value = np.mean(rolling_values) if rolling_values else float("nan")

    return {
        "name": name,
        "value": f"{round(numeric_value, 2)}",
        "avg": f"{round(avg_value, 2)}",
        "rate": get_rolling_rate(name),
        "unit": unit,
        "timestamp": timestamp,
    }
