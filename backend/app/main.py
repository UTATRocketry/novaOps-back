import asyncio
import json
from datetime import timedelta, datetime
import time
import uvicorn
import logging
import threading
import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, status, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from typing import List, Tuple
import mqtt_interface as mqtt
import data_file
import config_parser
from html_generator import generate_html, new_html, calibration_html
from auth import authenticate_user, create_access_token, get_current_user, ACCESS_TOKEN_EXPIRE_MINUTES
import dummy_pi
from dummy_pi import generate_data, handle_dummy_command, fake_processed_data

FAKE_DATA_FLAG = True


app = FastAPI()
logger = logging.getLogger('uvicorn.error')

# Allow CORS for React frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # To allow all --> allow_origins=["*"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Function to get token from WebSocket query parameters
async def get_token_from_websocket(websocket: WebSocket):
    try:
        token = websocket.query_params["token"]
        return token
    except KeyError:
        raise WebSocketDisconnect(code=1008)

@app.get("/")
async def get():
    return FileResponse("index.html")

@app.get("/view-data")
async def get_dataUI():
    return FileResponse("wsData.html")

@app.get("/view-commands")
async def get_commandUI():
    return FileResponse("commands.html")

@app.get("/entry")  # Temporary route for testing for frontend and backend integration
async def basic_test_endpoint():
    return {"message": "Hello World from Backend!"}

@app.get("/raw_data")
async def data_test_endpoint():
    return {"data" : mqtt.processed_data}  

@app.get("/start_saving_data")
async def start_saving_data():
    config_parser.SAVE_DATA_FLAG = True
    # create a CSV file with the current date and time in data folder
    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d_%H-%M-%S")
    config_parser.DATA_FILE = f"{timestamp}_data.csv"
    # write the header to the file: Timestamp then each sensor name in the config
    with open(f"logs/{config_parser.DATA_FILE}", 'w') as file:
        file.write("Timestamp,")
        for sensor in config_parser.get_config()["sensors"].values():
            file.write(f"{sensor['name']},")
        file.write("\n")
    return {"status": f"Saving data to {config_parser.DATA_FILE}"}

@app.get("/stop_saving_data")
async def stop_saving_data():
    config_parser.SAVE_DATA_FLAG = False
    #config_parser.DATA_FILE = None
    return {"status": "Stopped saving data"}

@app.get("/download_data_file")
async def download_data():
    # Download the CSV file
    if config_parser.DATA_FILE:
        return FileResponse(f"logs/{config_parser.DATA_FILE}", media_type='text/csv', filename=config_parser.DATA_FILE,  headers={"Content-Disposition": f"attachment; filename={config_parser.DATA_FILE}"})
    else:
        raise HTTPException(status_code=404, detail="No data file found")

@app.get("/dummy_data")
async def dummy_data_endpoint():
    return generate_data() 

@app.get("/dummy_command")
async def dummy_command_endpoint(command: dict):
    handle_dummy_command(command)
    return {"status": "Command sent"}

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
        with open(f"configs/{file.filename}",'w') as file:
            yaml.dump(new_config, file)
        # Load the new config into the app
        config_parser.CONFIG_FILE = f"configs/{file.filename}"
        config_parser.load_config()
        # Return a success message
    return {"status": "Config uploaded"}

@app.get("/update_config")
async def update_config_enpoint():
    config_parser.load_config()
    return {"status": "Config updated"}

@app.get("/actuators")
async def get_actuators():
    # Get the actuators from the config
    config = config_parser.get_config()
    actuators = []
    for relay in config["relays"].values():
        if relay.get("type") is None:
            continue
        actuator = {"type": "solenoid", "name": relay["name"]}
        actuators.append(actuator)
    for servo in config["servos"].values():
        actuator = {"type": "servo", "name": servo["name"]}
        actuators.append(actuator)
    return actuators

@app.get("/sensors")
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
            return mqtt.processed_data 
    except:
        return {} 

@app.websocket("/ws_basic")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    while True:
        #await mqtt.generate_sensor_data()
        await websocket.send_json(mqtt.processed_data)
        await asyncio.sleep(0.0000001)

@app.websocket("/ws")
async def websocket_endpoint2(websocket: WebSocket):
    # Accept the WebSocket connection
    await websocket.accept()

    # Get token from the query parameters
    token = await get_token_from_websocket(websocket)
    user = await get_current_user(token)
    try:
        while True:
            await websocket.send_json(mqtt.processed_data)
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


@app.post("/command")
async def send_command(command: dict):
    # Servos: {"id": "1", "angle": 90}
    # Relays: {"id": "1", "state": "0"}
    try:
        commands = await config_parser.convert_command(command)  # Convert command based on config
        for command in commands:
            # Publish each command to the MQTT broker
            #mqtt.publish_command(command)
            mqtt.mqtt_client.publish(mqtt.COMMAND_TOPIC, json.dumps(command))
            time.sleep(0.1)  # Add a small delay between commands to avoid flooding the broker
        return {"status": "Command sent"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/close_all")
async def close_all_endpoint():
    #status = mqtt.set_all_to_closed()
    #return status
    relay_command = {
        "type": "relay",
        "id": 0,
        "state": 0
    }
    #mqtt.mqtt_client.publish(mqtt.COMMAND_TOPIC, json.dumps(relay_command))
    status = await mqtt.set_all_to_closed()
    return {"status": "Commands sent"}
    
    

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
