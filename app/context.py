from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from app.services.clip_store import ClipStore
from app.services.command_service import CommandService
from app.services.config_history import ConfigHistory
from app.services.config_service import ConfigService
from app.services import pi_config
from app.services.data_service import RollingAverageStore, SensorParser
from app.services.mqtt_service import MqttService
from app.services.recording_service import RecordingService
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
    def __init__(
        self,
        config_path: Path,
        data_dir: Path,
        history_dir: Path | None = None,
        env: str = "",
        software: dict[str, Any] | None = None,
    ) -> None:
        self.runtime = RuntimeState()
        self.config_history = ConfigHistory(history_dir, env)
        self.config_service = ConfigService(config_path=config_path, history=self.config_history)
        self.ws_manager = WebSocketManager()
        self.role_service = RoleService(self.ws_manager)
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.recordings = RecordingService(data_dir, env=env, software=software)
        self.config_service.add_listener(self.recordings.config_changed)

        # Soundboard clips are staged in a temp dir and pulled by the bridge over
        # HTTP instead of riding an MQTT message (see services/clip_store.py).
        self.clip_store = ClipStore(
            ttl_s=float(os.getenv("NOVA_CLIP_TTL_S", "900")))

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
            stats_topic="nova/stats/#",
            state_topic="nova/state/#",
            on_stats_message=self._on_stats_message,
            on_state_message=self._on_state_message,
            heartbeat_topic=pi_config.HEARTBEAT_TOPIC,
            config_status_topic=pi_config.CONFIG_STATUS_FILTER,
            on_config_status_message=self._on_config_status_message,
            on_connected=self.publish_node_configs,
        )

        self.command_service = CommandService(self)

        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._staleness_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._heartbeat_seq = 0

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
        # Last time a stats message arrived per node. Stats are not retained, so a
        # node that stops publishing is dropped like stale telemetry.
        self._stats_last_seen: dict[str, float] = {}
        # Per Pi: when its config status last arrived, and (hash, time) of the
        # first publish of the config it should hold. Wall-clock ms, for the UI.
        self._pi_status_received_ms: dict[str, int] = {}
        self._node_config_published: dict[str, tuple[str | None, int]] = {}

    async def startup(self) -> None:
        self._event_loop = asyncio.get_running_loop()
        self.config_service.reload()
        # Record what we booted with: a config edited by hand on disk would
        # otherwise have no snapshot for a recording to point at.
        self.config_service.record_active("startup")
        self.mqtt_service.start()
        self.runtime.initialize_all(self.config_service.config)
        self._staleness_task = asyncio.create_task(self._staleness_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        LOGGER.info("Application started")

    async def shutdown(self) -> None:
        if self._staleness_task is not None:
            self._staleness_task.cancel()
            self._staleness_task = None
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        self.mqtt_service.stop()
        self._event_loop = None
        LOGGER.info("Application stopped")

    def set_data_saving(self, enabled: bool) -> str | None:
        """Start or stop a recording. Returns the new recording's name on start.

        The metadata file is written before the loggers are told to start, so
        there is never a data file without a record of its config.
        """
        name = None
        if enabled:
            name = self.recordings.start()
        else:
            self.recordings.stop()
        self.runtime.data_saving_enabled = enabled
        self.mqtt_service.publish_data_saving(enabled, filename=name)
        return name

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

    def _on_raw_message(self, topic: str, payload: Any) -> None:
        self._broadcast_from_mqtt({"type": "mqtt_message", "topic": topic, "payload": payload})

    @staticmethod
    def _node_from_topic(topic: str, prefix: str, allow_info: bool) -> tuple[str, bool] | None:
        """(node, is_info) for <prefix>/<node>, or <prefix>/<node>/info if allowed."""
        parts = topic.split("/")
        if len(parts) < 3 or "/".join(parts[:2]) != prefix or not parts[2]:
            return None
        if len(parts) == 3:
            return parts[2], False
        if allow_info and len(parts) == 4 and parts[3] == "info":
            return parts[2], True
        return None

    def _on_stats_message(self, topic: str, payload: dict[str, Any] | None) -> None:
        parsed = self._node_from_topic(topic, "nova/stats", allow_info=True)
        if parsed is None:
            return
        node, is_info = parsed
        if is_info:
            if payload is None:
                self.runtime.node_stats_info.pop(node, None)
            else:
                self.runtime.node_stats_info[node] = payload
            self._broadcast_from_mqtt({"type": "node_stats_info", "node": node, "info": payload})
            return
        if payload is None:
            self.runtime.node_stats.pop(node, None)
            self._stats_last_seen.pop(node, None)
        else:
            self.runtime.node_stats[node] = payload
            self._stats_last_seen[node] = time.monotonic()
        self._broadcast_from_mqtt({"type": "node_stats", "node": node, "stats": payload})

    def _on_state_message(self, topic: str, payload: dict[str, Any] | None) -> None:
        parsed = self._node_from_topic(topic, "nova/state", allow_info=False)
        if parsed is None:
            return
        node = parsed[0]
        if payload is None:
            self.runtime.node_state.pop(node, None)
        else:
            self.runtime.node_state[node] = payload
        self._broadcast_from_mqtt({"type": "node_state", "node": node, "state": payload})

    # --- per-Pi config, config status and heartbeat --------------------------

    def publish_node_configs(self) -> None:
        """Publish each Pi's retained nova/config/<node> from the active config.

        Called on every MQTT (re)connect and on every active-config change.
        """
        config = self.config_service.config
        config_hash = self.config_service.active_hash
        now_ms = int(time.time() * 1000)
        for node, message in pi_config.build_all_node_configs(config, config_hash).items():
            if self.mqtt_service.publish_node_config(node, message):
                # A republish of the same config (reconnect) keeps the first time.
                previous = self._node_config_published.get(node)
                if previous is None or previous[0] != config_hash:
                    self._node_config_published[node] = (config_hash, now_ms)

    def _pi_view(self, node: str, payload: dict[str, Any] | None) -> dict:
        config_hash = self.config_service.active_hash
        published = self._node_config_published.get(node)
        return pi_config.status_view(
            node, payload, self.config_service.config, config_hash,
            received_ms=self._pi_status_received_ms.get(node),
            published_ms=published[1] if published and published[0] == config_hash else None,
        )

    def pi_status_views(self) -> dict[str, dict]:
        return {node: self._pi_view(node, self.runtime.pi_config_status.get(node))
                for node in pi_config.TARGET_FOR_NODE}

    def pi_status_messages(self) -> list[dict[str, Any]]:
        return [{"type": "pi_config_status", "node": node, "status": view}
                for node, view in self.pi_status_views().items()]

    def apply_config_status(self, node: str, payload: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Store a Pi's config status and apply its trips. Returns the
        WebSocket messages to broadcast. Never sends a command."""
        if payload is None:
            self.runtime.pi_config_status.pop(node, None)
            self._pi_status_received_ms.pop(node, None)
        else:
            self.runtime.pi_config_status[node] = payload
            self._pi_status_received_ms[node] = int(time.time() * 1000)
        config = self.config_service.config
        messages: list[dict[str, Any]] = []
        if pi_config.apply_trips(config, self.runtime.pi_config_status, self.runtime.actuator_states,
                                 self.runtime.applied_trips, self.runtime.update_actuator_state):
            messages.append({"type": "actuator_states", "actuator_states": self.runtime.actuator_states})
        view = self._pi_view(node, payload)
        if view["mismatch"]:
            LOGGER.warning("%s config status mismatch: %s", node, ", ".join(view["mismatch_reasons"]))
        messages.insert(0, {"type": "pi_config_status", "node": node, "status": view})
        return messages

    async def _apply_config_status_async(self, node: str, payload: dict[str, Any] | None) -> None:
        for message in self.apply_config_status(node, payload):
            await self.broadcast(message)

    def _on_config_status_message(self, topic: str, payload: dict[str, Any] | None) -> None:
        node = pi_config.node_from_status_topic(topic)
        if node is None or node not in pi_config.TARGET_FOR_NODE:
            return
        if self._event_loop is None or not self._event_loop.is_running():
            # No loop to hand it to (tests, shutdown): update state only.
            self.apply_config_status(node, payload)
            return
        # Actuator state is otherwise only changed on the event loop.
        asyncio.run_coroutine_threadsafe(self._apply_config_status_async(node, payload), self._event_loop)

    async def _heartbeat_loop(self) -> None:
        """Tell the Pis the backend is alive. Runs on the main event loop on
        purpose: if the loop is stuck, the beats stop and the Pis go safe."""
        try:
            while True:
                if self.mqtt_service.publish_heartbeat(self._heartbeat_seq, int(time.time() * 1000)):
                    self._heartbeat_seq += 1
                await asyncio.sleep(pi_config.HEARTBEAT_INTERVAL_S)
        except asyncio.CancelledError:
            pass

    def _on_engine_message(self, payload: dict[str, Any]) -> None:
        source = str(payload.get("source", ""))
        sensors = payload.get("sensors", [])
        gpios = payload.get("gpios")

        # The bridge republishes its persistent state every cycle; if this
        # payload is byte-identical to the last one from the same source there is
        # nothing new — skip it so we don't re-average or re-broadcast stale data.
        if self._last_engine_raw.get(source) == (sensors, gpios):
            return
        self._last_engine_raw[source] = (sensors, gpios)

        parsed = self.sensor_parser.parse(
            source=source,
            raw_sensors=sensors,
            config=self.config_service.config,
            calibration_enabled=self.runtime.calibration_enabled,
            gpios=gpios if isinstance(gpios, list) else None,
        )

        now = time.monotonic()
        changed = False
        for item in parsed:
            entry = item.to_dict()
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

                stale_nodes = [
                    node for node, seen in self._stats_last_seen.items()
                    if now - seen > STALE_TTL_S
                ]
                for node in stale_nodes:
                    self._stats_last_seen.pop(node, None)
                    self.runtime.node_stats.pop(node, None)
                    LOGGER.info("Cleared stale stats for %s (no update in %.0fs)", node, STALE_TTL_S)
                    await self.broadcast({"type": "node_stats", "node": node, "stats": None})

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
        # A soundboard clip upload ack completes the waiting /fas/sound/upload
        # request; it is still rebroadcast so clients can show the outcome.
        if str(payload.get("type", "")) == "sound_upload_result":
            self.command_service.resolve_sound_upload(payload)
        self._broadcast_from_mqtt(payload)

    def _on_control_message(self, payload: dict[str, Any]) -> None:
        if str(payload.get("source", "")).strip().lower() != "novalock":
            return
        self.runtime.lockout_state = str(payload.get("state", "unlocked"))
        self._broadcast_from_mqtt({"type": "lockout", "state": self.runtime.lockout_state})
