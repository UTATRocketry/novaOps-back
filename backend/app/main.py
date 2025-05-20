import asyncio
import json
from datetime import timedelta
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, status
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
from dummy_pi import generate_data, handle_dummy_command

app = FastAPI()

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
    return FileResponse("wsData.html")

@app.get("/entry")  # Temporary route for testing for frontend and backend integration
async def basic_test_endpoint():
    return {"message": "Hello World from Backend!"}

@app.get("/raw_data")
async def data_test_endpoint():
    return {"data" : mqtt.processed_data}  

@app.get("/dummy_data")
async def dummy_data_endpoint():
    return generate_data() 

@app.get("/dummy_command")
async def dummy_command_endpoint(command: dict):
    handle_dummy_command(command)
    return {"status": "Command sent"}

@app.get("/update_config")
async def update_config_enpoint():
    config_parser.load_config()
    return {"status": "Config updated"}

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