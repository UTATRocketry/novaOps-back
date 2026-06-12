from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from app.services.command_service import CommandService
from app.services.config_service import ConfigService
from app.services.data_service import RollingAverageStore, SensorParser
from app.services.mqtt_service import MqttService
from app.services.role_service import RoleService
from app.state import RuntimeState
from app.websocket_manager import WebSocketManager

LOGGER = logging.getLogger(__name__)


class AppContext:
    def __init__(self, config_path: Path, data_dir: Path) -> None:
        self.runtime = RuntimeState()
        self.config_service = ConfigService(config_path=config_path)
        self.ws_manager = WebSocketManager()
        self.role_service = RoleService(self.ws_manager)
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

        rolling_store = RollingAverageStore(window_size=5)
        self.sensor_parser = SensorParser(rolling_store)

        self.mqtt_service = MqttService(
            sensor_topic="nova/telemetry/engine",
            command_topic="nova/command",
            control_topic="nova/control",
            flight_topic="nova/telemetry/flight",
            console_topic="nova/console",
            on_sensor_message=self._on_engine_message,
            on_flight_message=self._on_flight_message,
            on_console_message=self._on_console_message,
            on_control_message=self._on_control_message,
        )

        self.command_service = CommandService(self)

        self._event_loop: asyncio.AbstractEventLoop | None = None

    async def startup(self) -> None:
        self._event_loop = asyncio.get_running_loop()
        self.config_service.reload()
        self.mqtt_service.start()
        self.runtime.initialize_all(self.config_service.config)
        LOGGER.info("Application started")

    async def shutdown(self) -> None:
        self.mqtt_service.stop()
        self._event_loop = None
        LOGGER.info("Application stopped")

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """Broadcast a payload to all connected clients.

        Cleans up role state for any clients that fail to receive the message.
        Use this as the single broadcast entry point instead of ws_manager.broadcast directly.
        """
        removed_ids = await self.ws_manager.broadcast(payload)
        for cid in removed_ids:
            await self.role_service.on_disconnect(cid)

    def _broadcast_from_mqtt(self, payload: dict[str, Any]) -> None:
        if self._event_loop is None or not self._event_loop.is_running():
            LOGGER.warning("No running event loop available for websocket broadcast")
            return
        asyncio.run_coroutine_threadsafe(self.broadcast(payload), self._event_loop)

    def _on_engine_message(self, payload: dict[str, Any]) -> None:
        source = str(payload.get("source", ""))
        sensors = payload.get("sensors", [])
        parsed = self.sensor_parser.parse(
            source=source,
            raw_sensors=sensors,
            config=self.config_service.config,
            calibration_enabled=self.runtime.calibration_enabled,
        )
        self.runtime.latest_engine_data = [item.__dict__ for item in parsed]
        self.runtime.latest_sensors = self.runtime.latest_engine_data
        self._broadcast_from_mqtt({"type": "engine_data", "data": self.runtime.latest_engine_data})
        self._broadcast_from_mqtt({"type": "parsed_data", "sensors": self.runtime.latest_sensors})

    def _on_flight_message(self, payload: dict[str, Any]) -> None:
        data = payload.get("data")
        events = payload.get("events")
        if isinstance(data, dict):
            self.runtime.latest_flight_data = data
            self._broadcast_from_mqtt({"type": "flight_data", "data": self.runtime.latest_flight_data})
        if isinstance(events, list):
            self.runtime.latest_events = events
            self._broadcast_from_mqtt({"type": "flight_events", "events": self.runtime.latest_events})

    def _on_console_message(self, payload: dict[str, Any]) -> None:
        self._broadcast_from_mqtt(payload)

    def _on_control_message(self, payload: dict[str, Any]) -> None:
        if str(payload.get("source", "")).strip().lower() != "novalock":
            return
        self.runtime.lockout_state = str(payload.get("state", "unlocked"))
        self._broadcast_from_mqtt({"type": "lockout", "state": self.runtime.lockout_state})
