import asyncio
import json
from datetime import timedelta, datetime
import time
import uvicorn
import logging
import threading
import yaml
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Depends, HTTPException, status, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List, Tuple
import mqtt_interface as mqtt
import data_file
import config_parser
import data_interface
import command_interface
from html_generator import generate_html, new_html, calibration_html
from auth import authenticate_user, create_access_token, get_current_user, ACCESS_TOKEN_EXPIRE_MINUTES
import dummy_pi
from dummy_pi import generate_data, handle_dummy_command, fake_processed_data

FAKE_DATA_FLAG = True


app = FastAPI()
logger = logging.getLogger('uvicorn.error')

app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

# Allow CORS for React frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # To allow all --> allow_origins=["*"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

config_parser.load_config()  # Load the initial configuration
# Initialize MQTT client
#mqtt.init_mqtt_client()
# Initialize data interface
#data_interface.init_data_interface()
# Initialize command interface
#command_interface.init_command_interface()
#command_interface.initialize_actuator_states()
# Initialize dummy data generation if enabled
#if FAKE_DATA_FLAG:

# Function to get token from WebSocket query parameters
async def get_token_from_websocket(websocket: WebSocket):
    try:
        token = websocket.query_params["token"]
        return token
    except KeyError:
        raise WebSocketDisconnect(code=1008)

@app.get("/", response_class=HTMLResponse)
async def get(request: Request):
    #return FileResponse("static/index.html")
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/view-json")
async def get_dataUI():
    return FileResponse("static/wsJSON.html")

@app.get("/view-table")
async def get_dataUI():
    return FileResponse("static/wsTable.html")

@app.get("/view-table2")
async def get_dataUI():
    return FileResponse("static/table.html")

@app.get("/view-commands")
async def get_commandUI():
    return FileResponse("static/commands.html")

@app.get("/view-gui")
async def get_commandUI():
    return FileResponse("static/index2.html")

@app.get("/entry")  # Temporary route for testing for frontend and backend integration
async def basic_test_endpoint():
    return {"message": "Hello World from Backend!"}

@app.get("/raw_data")
async def data_test_endpoint():
    return {"data" : mqtt.raw_data}  

@app.post("/toggle_calibration")
async def toggle_calibration(command: dict):
    """
    Set the calibration flag to True or False.
    """
    flag = command.get("calibration")  # Default to toggling the current state
    if flag is None:
        #flag = not data_interface.CALIBRATION_FLAG  # Toggle the current state if no flag is provided
        raise HTTPException(status_code=400, detail="Flag parameter is required")
    if not isinstance(flag, bool):
        raise HTTPException(status_code=400, detail="Flag must be a boolean value")
    # Set the calibration flag in the config parser
    if data_interface.CALIBRATION_FLAG == flag:
        raise HTTPException(status_code=400, detail="Calibration mode is already set to this value")
    print(f"Setting calibration mode to {flag}")
    #data_interface.CALIBRATION_FLAG = not data_interface.CALIBRATION_FLAG
    data_interface.CALIBRATION_FLAG = flag
    data_interface.data_store = {}
    if flag:
        return {"status": "Calibration mode enabled"}
    else:
        return {"status": "Calibration mode disabled"}
    
@app.get("/start_saving_data")
async def start_saving_data():
    data_interface.new_data_file()
    data_interface.SAVE_DATA_FLAG = True
    command_interface.new_log_file()
    command_interface.SAVE_LOG_FLAG = True
    return {"status": f"Saving data to {data_interface.DATA_FILE}"}


@app.get("/stop_saving_data")
async def stop_saving_data():
    data_interface.SAVE_DATA_FLAG = False
    #data_interface.DATA_FILE = None
    return {"status": "Stopped saving data"}


@app.get("/toggle_saving_data")
async def toggle_saving_data():
    if not data_interface.SAVE_DATA_FLAG:
        data_interface.new_data_file()
        data_interface.SAVE_DATA_FLAG = True
        return {"status": f"Saving data to {data_interface.DATA_FILE}"}
    else:
        data_interface.SAVE_DATA_FLAG = False
        return {"status": "Stopped saving data"}

@app.get("/download_data_file")
async def download_data():
    # Download the CSV file
    if data_interface.DATA_FILE:
        return FileResponse(f"logs/{data_interface.DATA_FILE}", media_type='text/csv', filename=data_interface.DATA_FILE,  headers={"Content-Disposition": f"attachment; filename={data_interface.DATA_FILE}"})
    else:
        raise HTTPException(status_code=404, detail="No data file found")

@app.get("/dummy_data")
async def dummy_data_endpoint():
    return generate_data() 

@app.get("/dummy_command")
async def dummy_command_endpoint(command: dict):
    handle_dummy_command(command)
    return {"status": "Command sent"}

@app.get("/get_config")
async def get_config_endpoint():
    # Get the current configuration
    config = config_parser.get_config()
    if not config:
        raise HTTPException(status_code=404, detail="Configuration not found")
    return config

@app.get("/upload_config")
async def upload_config_endpoint(file: UploadFile):
    if not file:
        return {"message": "No upload file sent"}
    else:
        # Read the contents of the uploaded file
        contents = await file.read()
        # Decode the contents from bytes to string
        config_str = contents.decode('utf-8')
        # Parse the YAML string into a Python dictionary
        new_config = yaml.safe_load(config_str)

        # Validate the new configuration
        if not new_config or not isinstance(new_config, dict):
            raise HTTPException(status_code=400, detail="Invalid configuration format")
        # Update the configuration using the config_parser
        config_parser.update_config(file.filename, new_config)
        # Return a success message
    return {"status": "Config uploaded"}

@app.get("/update_config")
async def update_config_enpoint():
    config_parser.load_config()
    return {"status": "Config updated"}

@app.get("/get_actuators")
async def get_actuators():
    """
    # Get the actuators from the config
    config = config_parser.get_config()
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
        actuator = {"type": "servo", "name": servo["name"]}
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
    """
    return config_parser.get_actuators_config()

@app.get("/get_sensors")
async def get_sensors():
    # Get the sensors from the config
    config = config_parser.get_config()
    sensors = {"sensors": config["sensors"]}
    # Return the sensors as JSON
    return sensors

# TODO: use ORJSON for the data stream
@app.get("/front")
async def get_actuator_data():
    try:
        while True:
            #await generate_sensor_data()
            #await get_timestamp()
            return data_interface.processed_data 
    except:
        return {} 

@app.websocket("/ws_raw_data")
async def websocket_basic_endpoint(websocket: WebSocket):
    await websocket.accept()
    while True:
        #await mqtt.generate_sensor_data()
        await websocket.send_json(mqtt.raw_data)
        await asyncio.sleep(0.1)

@app.websocket("/ws_basic")
async def websocket_basic_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            #await mqtt.generate_sensor_data()
            ws_data = data_interface.format_for_ws()
            await websocket.send_json(ws_data)
            await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        print(f"Client disconnected")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(data_interface.processed_data)
            # Check if any commands are received
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=0.5)
                command = json.loads(message)

                # Handle actuator toggling
                print(command)

            except asyncio.TimeoutError:
                # Continue sending sensor updates if no command is received
                pass

            # Wait for 1 second before sending the next update
            await asyncio.sleep(0.1)

    except WebSocketDisconnect:
        print(f"Client disconnected")

@app.websocket("/ws_auth")
async def websocket_endpoint2(websocket: WebSocket):
    # Accept the WebSocket connection
    await websocket.accept()

    # Get token from the query parameters
    token = await get_token_from_websocket(websocket)
    user = await get_current_user(token)
    try:
        while True:
            await websocket.send_json(data_interface.processed_data)
            # Check if any commands are received
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                command = json.loads(message)

                # Handle actuator toggling
                await handle_dummy_command(command)

            except asyncio.TimeoutError:
                # Continue sending sensor updates if no command is received
                pass

            # Wait for 1 second before sending the next update
            await asyncio.sleep(0.01)

    except WebSocketDisconnect:
        print(f"Client {user['username']} disconnected")


@app.post("/send_command")
async def send_command(command: dict):
    # Servos: {"id": "1", "angle": 90}
    # Relays: {"id": "1", "state": "0"}
    # print(f"Received command: {command}")
    try:
        #command_interface.validate_command(command)  # Validate command structure
        # Update processed data with the new actuator state
        command_list = await command_interface.convert_command(command)  # Convert command based on config
        for parsed_command in command_list:
            # Publish each command to the MQTT broker
            #mqtt.publish_command(command)
            if parsed_command is None:
                raise HTTPException(status_code=400, detail="Invalid command format")
            mqtt.mqtt_client.publish(mqtt.COMMAND_TOPIC, json.dumps(parsed_command))
            await asyncio.sleep(0.01)  # Add a small delay between commands to avoid flooding the broker

        # Update the actuator states with the new command
        #try:
        #    command_interface.update_actuator_state(command['name'], command['state'])
        #except Exception as e:
        #    print(f"Error updating actuator state: {e}")

        if command_interface.SAVE_LOG_FLAG:
            command_interface.save_log(command)
        return {"status": "Command sent"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/close_all")
async def close_all_endpoint():
    status = await command_interface.set_all_to_closed()
    """
    for i in range(16):
        command = {
            "type": "relay",
            "id": i,
            "state": 1
        }
        mqtt.mqtt_client.publish(mqtt.COMMAND_TOPIC, json.dumps(command))
        time.sleep(0.1)  # Add a small delay between commands to avoid flooding the broker
    """
    return {"status": "Commands sent"}
    
@app.get("/open_all")
async def open_all_endpoint():
    #status = mqtt.set_all_to_closed()
    for i in range(16):
        command = {
            "type": "relay",
            "id": i,
            "state": 0
        }
        mqtt.mqtt_client.publish(mqtt.COMMAND_TOPIC, json.dumps(command))
        time.sleep(0.1)  # Add a small delay between commands to avoid flooding the brokerr
    return {"status": "Commands sent"} 

@app.get("/set_to_defaults")
async def set_to_defaults_endpoint():
    try:
        mqtt.set_to_defaults()
        return {"status": "Default commands sent"}
    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail=str(e))
    
    
# Data model for calibration update
class CalibrationRequest(BaseModel):
    sensor: str
    calibration: List[Tuple[float, float]]  # List of (voltage, unit value) pairs

@app.post("/calibrate_sensor")
async def calibrate_sensor(request: CalibrationRequest):
    # Mock logic to store calibration (replace with actual DB/store logic)
    print(f"Received calibration for {request.sensor}: {request.calibration}")

    if not request.calibration:
        raise HTTPException(status_code=400, detail="Calibration data is empty")
    else:
        # config_parser.update_calibration(request.sensor, request.calibration)
        # update_calibration(sensor_str, calibration_points)
        return {"message": "Calibration updated successfully", "sensor": request.sensor}

# Token route for login
@app.post("/token")
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user['username']}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
