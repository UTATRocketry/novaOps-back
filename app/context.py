from __future__ import annotations

import asyncio
from pathlib import Path

from app.config_service import ConfigService
from app.mqtt_service import MqttService
from app.parsing import CommandParser, RollingAverageStore, SensorParser
from app.models import CommandPayload
from app.state import RuntimeState
from app.websocket_manager import WebSocketManager


class AppContext:
    def __init__(self, config_path: Path, data_dir: Path) -> None:
        self.runtime = RuntimeState()
        self.config_service = ConfigService(config_path=config_path)
        self.rolling_store = RollingAverageStore(window_size=5)
        self.sensor_parser = SensorParser(self.rolling_store)
        self.websocket_manager = WebSocketManager()
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.mqtt_service = MqttService(
            sensor_topic="nova/telemetry",
            command_topic="nova/commands",
            control_topic="nova/control",
            on_sensor_message=self._on_sensor_message,
        )

    @property
    def command_parser(self) -> CommandParser:
        return CommandParser(self.config_service.config)

    def _on_sensor_message(self, payload: dict) -> None:
        source = str(payload.get("source", ""))
        sensors = payload.get("sensors", [])
        parsed = self.sensor_parser.parse(
            source=source,
            raw_sensors=sensors,
            config=self.config_service.config,
            calibration_enabled=self.runtime.calibration_enabled,
        )
        parsed_payload = {
            "type": "parsed_data",
            "source": source,
            "sensors": [item.__dict__ for item in parsed],
        }
        self.runtime.latest_sensors = parsed_payload["sensors"]

        # MQTT callback can run off the event loop thread.
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.websocket_manager.broadcast(parsed_payload))
        except RuntimeError:
            pass

    def execute_command(self, command: dict) -> list[dict]:
        payload = CommandPayload.model_validate(command)
        parsed_commands = self.command_parser.parse(payload)
        self.mqtt_service.publish_device_commands(parsed_commands)
        self.runtime.actuator_states[command["name"]] = command["state"]
        return parsed_commands
