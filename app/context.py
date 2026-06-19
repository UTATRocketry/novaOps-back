from __future__ import annotations

import asyncio
import logging
import os
import time
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

# Telemetry is considered stale if nothing fresh arrives within this many seconds.
# A sensor whose timestamp stops advancing (or a flight feed that stops arriving)
# is dropped from the cache and a clearing update is broadcast to clients.
STALE_TTL_S = float(os.getenv("NOVA_STALE_TTL_S", "10"))
STALE_CHECK_INTERVAL_S = float(os.getenv("NOVA_STALE_CHECK_INTERVAL_S", "2"))


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
            on_raw_message=self._on_raw_message,
        )

        self.command_service = CommandService(self)

        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._staleness_task: asyncio.Task | None = None

        # Freshness tracking. Engine sensors are kept by name with the monotonic
        # time their reading last advanced; flight tracks the last message time.
        # The fas_bridge republishes its persistent state every cycle, so we
        # dedup identical payloads per source to avoid flooding the WebSocket.
        self._engine_current: dict[str, dict] = {}
        self._engine_last_change: dict[str, float] = {}
        # name -> (timestamp, value) of the last reading actually ingested. Kept
        # even after a sensor is pruned so a device that keeps republishing the
        # same frozen reading can't resurrect it (avoids a clear/refill loop).
        self._engine_last_seen: dict[str, tuple] = {}
        self._last_engine_raw: dict[str, Any] = {}
        self._flight_last_seen: float | None = None

    async def startup(self) -> None:
        self._event_loop = asyncio.get_running_loop()
        self.config_service.reload()
        self.mqtt_service.start()
        self.runtime.initialize_all(self.config_service.config)
        self._staleness_task = asyncio.create_task(self._staleness_loop())
        LOGGER.info("Application started")

    async def shutdown(self) -> None:
        if self._staleness_task is not None:
            self._staleness_task.cancel()
            self._staleness_task = None
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

    def _on_raw_message(self, topic: str, payload: dict[str, Any]) -> None:
        self._broadcast_from_mqtt({"type": "mqtt_message", "topic": topic, "payload": payload})

    def _on_engine_message(self, payload: dict[str, Any]) -> None:
        source = str(payload.get("source", ""))
        sensors = payload.get("sensors", [])

        # The bridge republishes its persistent state every cycle; if this
        # payload is byte-identical to the last one from the same source there is
        # nothing new — skip it so we don't re-average or re-broadcast stale data.
        if self._last_engine_raw.get(source) == sensors:
            return
        self._last_engine_raw[source] = sensors

        parsed = self.sensor_parser.parse(
            source=source,
            raw_sensors=sensors,
            config=self.config_service.config,
            calibration_enabled=self.runtime.calibration_enabled,
        )

        now = time.monotonic()
        changed = False
        for item in parsed:
            entry = item.__dict__
            name = entry["name"]
            seen = (entry.get("timestamp"), entry.get("value"))
            # A reading identical to the last one ingested (same timestamp and
            # value) is a frozen republish — ignore it so it neither refreshes
            # freshness nor resurrects a sensor the staleness loop pruned.
            if self._engine_last_seen.get(name) == seen:
                continue
            self._engine_last_seen[name] = seen
            self._engine_last_change[name] = now
            self._engine_current[name] = entry
            changed = True

        if changed:
            snapshot = list(self._engine_current.values())
            self.runtime.latest_engine_data = snapshot
            self.runtime.latest_sensors = snapshot
            self._broadcast_from_mqtt({"type": "engine_data", "data": snapshot})

    def _on_flight_message(self, payload: dict[str, Any]) -> None:
        data = payload.get("data")
        events = payload.get("events")
        if isinstance(data, dict):
            self._flight_last_seen = time.monotonic()
            self.runtime.latest_flight_data = data
            self._broadcast_from_mqtt({"type": "flight_data", "data": self.runtime.latest_flight_data})
        if isinstance(events, list):
            self.runtime.latest_events = events
            self._broadcast_from_mqtt({"type": "flight_events", "events": self.runtime.latest_events})

    async def _staleness_loop(self) -> None:
        """Drop telemetry that has stopped updating so clients (including ones
        that connect later and replay the cache) don't see frozen values."""
        try:
            while True:
                await asyncio.sleep(STALE_CHECK_INTERVAL_S)
                now = time.monotonic()

                stale = [
                    name for name, seen in self._engine_last_change.items()
                    if now - seen > STALE_TTL_S
                ]
                if stale:
                    for name in stale:
                        self._engine_current.pop(name, None)
                        self._engine_last_change.pop(name, None)
                        # Keep _engine_last_seen so a frozen republish of the same
                        # reading does not re-add the sensor we just pruned.
                    snapshot = list(self._engine_current.values())
                    self.runtime.latest_engine_data = snapshot
                    self.runtime.latest_sensors = snapshot
                    LOGGER.info("Cleared %d stale engine sensor(s): %s", len(stale), ", ".join(stale))
                    await self.broadcast({"type": "engine_data", "data": snapshot})

                if (self._flight_last_seen is not None
                        and self.runtime.latest_flight_data
                        and now - self._flight_last_seen > STALE_TTL_S):
                    self.runtime.latest_flight_data = {}
                    self._flight_last_seen = None
                    LOGGER.info("Cleared stale flight data (no update in %.0fs)", STALE_TTL_S)
                    await self.broadcast({"type": "flight_data", "data": {}})
        except asyncio.CancelledError:
            pass

    def _on_console_message(self, payload: dict[str, Any]) -> None:
        self._broadcast_from_mqtt(payload)

    def _on_control_message(self, payload: dict[str, Any]) -> None:
        if str(payload.get("source", "")).strip().lower() != "novalock":
            return
        self.runtime.lockout_state = str(payload.get("state", "unlocked"))
        self._broadcast_from_mqtt({"type": "lockout", "state": self.runtime.lockout_state})
