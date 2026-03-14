from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.config_service import ConfigService

from app.models import ActuatorConfig, ActuatorType, CommandPayload, FlagPayload
from app.mqtt_service import MqttService
from app.parsing import CommandParser, RollingAverageStore, SensorParser
from app.state import RuntimeState
from app.websocket_manager import WebSocketManager


def configure_logging(base_dir: Path) -> None:
    log_dir = base_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    if root.handlers:
        return

    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    file_handler = RotatingFileHandler(log_dir / "server.log", maxBytes=2_000_000, backupCount=3)
    file_handler.setFormatter(formatter)

    root.addHandler(console)
    root.addHandler(file_handler)


def create_app() -> FastAPI:
    base_dir = Path(__file__).resolve().parent.parent
    configure_logging(base_dir)
    logger = logging.getLogger("novaops")

    config_path = base_dir / "config" / "system.yaml"
    data_dir = base_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    config_service = ConfigService(config_path)
    rolling_store = RollingAverageStore(window_size=5)
    sensor_parser = SensorParser(rolling_store)
    ws_manager = WebSocketManager()
    runtime = RuntimeState()
    event_loop: asyncio.AbstractEventLoop | None = None

    openapi_tags = [
        {"name": "System", "description": "Health and service status endpoints."},
        {"name": "Config", "description": "Load, view, and update YAML-backed system configuration."},
        {"name": "Flags", "description": "Runtime feature flags such as calibration and data saving."},
        {"name": "Data", "description": "CSV data file listing and download endpoints."},
        {"name": "Commands", "description": "Actuator command translation and MQTT publish endpoints."},
    ]

    app = FastAPI(
        title="NovaOps Backend",
        version="2.0.0",
        summary="FastAPI backend for NovaOps device integration",
        description=(
            "Provides configuration-driven parsing, MQTT bridge logic, actuator command translation, "
            "and websocket streaming for NovaOps clients."
        ),
        openapi_tags=openapi_tags,
    )

    static_dir = base_dir / "app" / "static"
    templates_dir = base_dir / "app" / "templates"

    templates = Jinja2Templates(directory=str(templates_dir)) if templates_dir.exists() else None

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Allow CORS for React frontend
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # To allow all --> allow_origins=["*"]
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def on_sensor_message(payload: dict[str, Any]) -> None:
        source = str(payload.get("source", ""))
        sensors = payload.get("sensors", [])
        parsed = sensor_parser.parse(
            source=source,
            raw_sensors=sensors,
            config=config_service.config,
            calibration_enabled=runtime.calibration_enabled,
        )
        runtime.latest_sensors = [entry.__dict__ for entry in parsed]

        message = {
            "type": "parsed_data",
            "source": source,
            "sensors": runtime.latest_sensors,
        }

        if event_loop is None or not event_loop.is_running():
            logger.warning("No running event loop available for websocket broadcast")
            return

        asyncio.run_coroutine_threadsafe(ws_manager.broadcast(message), event_loop)

    mqtt_service = MqttService(
        sensor_topic="nova/telemetry",
        command_topic="nova/commands",
        control_topic="nova/control",
        on_sensor_message=on_sensor_message,
    )
    
    def find_actuator(name: str) -> ActuatorConfig | None:
        for actuator in config_service.config.all_actuators():
            if actuator.name == name:
                return actuator
        return None

    def update_actuator_state(actuator: ActuatorConfig, state: str) -> None:
        entry = runtime.actuator_states.get(actuator.name)
        if not isinstance(entry, dict):
            entry = {}

        lower = state.strip().lower()
        if actuator.actuator_type in {ActuatorType.SERVO, ActuatorType.SERVO3}:
            if lower in {"enable", "enabled", "disable", "disabled"}:
                entry["enable"] = "enabled" if lower in {"enable", "enabled"} else "disabled"
            elif lower in {"on", "off"}:
                entry["power"] = lower
            else:
                entry["position"] = state
        elif actuator.actuator_type == ActuatorType.SOLENOID:
            if lower in {"open", "closed"}:
                entry["position"] = lower
            else:
                entry["position"] = state
        elif actuator.actuator_type == ActuatorType.POWERED_GPIO_DEVICE:
            if lower in {"on", "off"}:
                entry["power"] = lower
            if lower in {"armed", "disarmed"}:
                entry["arming"] = lower
        else:
            if lower in {"on", "off"}:
                entry["power"] = lower
            if lower in {"armed", "disarmed"}:
                entry["arming"] = lower

        runtime.actuator_states[actuator.name] = entry
    
    def initialize_actuators() -> None:
        for actuator in config_service.config.all_actuators():
            init_state = {}
            if actuator.actuator_type in {ActuatorType.SERVO, ActuatorType.SERVO3}:
                if actuator.default_position is not None:
                     init_state["position"] = actuator.default_position
                init_state["enable"] = "disabled"
                init_state["power"] = "off"
            elif actuator.actuator_type == ActuatorType.SOLENOID:
                init_state["position"] = "closed"
            else:
                init_state["power"] = "off"
                init_state["arming"] = "disarmed"
            runtime.actuator_states[actuator.name] = init_state

    @app.on_event("startup")
    async def startup_event() -> None:
        nonlocal event_loop
        event_loop = asyncio.get_running_loop()
        config_service.reload()
        mqtt_service.start()
        initialize_actuators()
        logger.info("Application started")

    @app.on_event("shutdown")
    async def shutdown_event() -> None:
        nonlocal event_loop
        mqtt_service.stop()
        event_loop = None
        logger.info("Application stopped")

    def command_parser() -> CommandParser:
        return CommandParser(config_service.config)

    async def apply_command(command: CommandPayload) -> list[dict]:
        parser = command_parser()
        parsed_commands = parser.parse(command)
        mqtt_service.publish_device_commands(parsed_commands)
        actuator = find_actuator(command.name)
        if actuator is not None:
            update_actuator_state(actuator, command.state)
        else:
            runtime.actuator_states[command.name] = {"state": command.state}
        await ws_manager.broadcast(
            {
                "type": "actuator_states",
                "actuator_states": runtime.actuator_states,
            }
        )
        return parsed_commands
    
    @app.get("/", response_class=HTMLResponse)
    async def get(request: Request):
        if templates is None:
            return HTMLResponse("<h1>NovaOps Backend</h1><p>API is running.</p>")
        return templates.TemplateResponse("index.html", {"request": request})

    @app.get("/view-json")
    async def get_data_ui():
        file_path = static_dir / "wsJSON.html"
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="UI file not found")
        return FileResponse(file_path)
    
    @app.get("/health", tags=["System"], summary="Health check", description="Simple liveness endpoint.")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/actuators", tags=["Config"], summary="List actuators", description="Return actuator definitions from loaded config.")
    async def get_actuators() -> list[dict]:
        return [item.model_dump(by_alias=True) for item in config_service.config.all_actuators()]

    @app.get("/api/sensors", tags=["Config"], summary="List sensors", description="Return sensor definitions from loaded config.")
    async def get_sensors() -> list[dict]:
        return [item.model_dump(by_alias=True) for item in config_service.config.all_sensors()]

    @app.get("/api/config", tags=["Config"], summary="Get full config", description="Return the active configuration as JSON.")
    async def get_config() -> dict:
        return config_service.config.model_dump(by_alias=True)

    @app.post("/api/config/upload", tags=["Config"], summary="Upload config file", description="Upload and activate a YAML config file.")
    async def upload_config(file: UploadFile = File(...)) -> dict:
        payload = await file.read()
        config = config_service.upload_config_bytes(payload)
        return config.model_dump(by_alias=True)

    @app.put("/api/config", tags=["Config"], summary="Replace config", description="Replace the active configuration with supplied JSON payload.")
    async def replace_config(config_payload: dict) -> dict:
        config = config_service.update_config(config_payload)
        return config.model_dump(by_alias=True)

    @app.patch("/api/config", tags=["Config"], summary="Patch config", description="Shallow-merge a JSON payload into the active configuration.")
    async def patch_config(config_payload: dict) -> dict:
        current = config_service.config.model_dump(by_alias=True)
        current.update(config_payload)
        config = config_service.update_config(current)
        return config.model_dump(by_alias=True)

    @app.post("/api/config/reload", tags=["Config"], summary="Reload config", description="Reload config from disk path configured at startup.")
    async def reload_config() -> dict:
        config = config_service.reload()
        return config.model_dump(by_alias=True)

    @app.post("/api/flags/calibration", tags=["Flags"], summary="Set calibration flag", description="Enable or disable calibrated sensor output.")
    async def set_calibration_flag(payload: FlagPayload) -> dict:
        runtime.calibration_enabled = payload.enabled
        return {"calibration_enabled": runtime.calibration_enabled}

    @app.post("/api/flags/data-saving", tags=["Flags"], summary="Set data saving flag", description="Start/stop logger commands over MQTT.")
    async def set_data_saving_flag(payload: FlagPayload) -> dict:
        runtime.data_saving_enabled = payload.enabled
        mqtt_service.publish_data_saving(payload.enabled)
        return {"data_saving_enabled": runtime.data_saving_enabled}

    @app.get("/api/data-files", tags=["Data"], summary="List CSV files", description="List available CSV files from local data directory.")
    async def list_data_files() -> list[str]:
        return sorted(path.name for path in data_dir.glob("*.csv"))

    @app.get("/api/data-files/{file_name}", tags=["Data"], summary="Download CSV file", description="Download a CSV file by file name.")
    async def get_data_file(file_name: str) -> FileResponse:
        file_path = data_dir / file_name
        if not file_path.exists() or file_path.suffix.lower() != ".csv":
            raise HTTPException(status_code=404, detail="CSV file not found")
        return FileResponse(file_path)

    @app.post("/api/commands", tags=["Commands"], summary="Send actuator command", description="Translate and publish a client actuator command over MQTT.")
    async def post_command(payload: CommandPayload) -> dict:
        try:
            commands = await apply_command(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"published_commands": commands}

    async def handle_websocket(websocket: WebSocket) -> None:
        assigned_role = await ws_manager.connect(websocket, None)

        await ws_manager.send_json(
            websocket,
            {
                "type": "session",
                "role": assigned_role,
            },
        )
        await ws_manager.snapshot(websocket, runtime.actuator_states)

        if runtime.latest_sensors:
            await ws_manager.send_json(
                websocket,
                {
                    "type": "parsed_data",
                    "sensors": runtime.latest_sensors,
                },
            )

        try:
            while True:
                payload = await websocket.receive_json()
                command = CommandPayload.model_validate(payload)
                try:
                    await apply_command(command)
                except ValueError as exc:
                    await ws_manager.send_json(websocket, {"type": "error", "detail": str(exc)})
        except WebSocketDisconnect:
            await ws_manager.disconnect(websocket)

    @app.websocket("/ws_basic")
    async def websocket_basic_endpoint(websocket: WebSocket) -> None:
        await handle_websocket(websocket)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await handle_websocket(websocket)

    return app


app = create_app()
