from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Callable

from app.models import CommandPayload

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover
    mqtt = None

LOGGER = logging.getLogger(__name__)
DATA_PATH = "/home/admin/Desktop"

class MqttService:
    def __init__(
        self,
        sensor_topic: str,
        command_topic: str,
        control_topic: str,
        on_sensor_message: Callable[[dict], None],
    ) -> None:
        self._sensor_topic = sensor_topic
        self._command_topic = command_topic
        self._control_topic = control_topic
        self._on_sensor_message = on_sensor_message
        self._client = None
        self._connected = False
        self._file_num = 0
        self._data_file = None

    def start(self) -> None:
        if mqtt is None:
            LOGGER.warning("paho-mqtt not available, MQTT disabled")
            return

        broker = os.getenv("NOVA_MQTT_BROKER", "localhost")
        port = int(os.getenv("NOVA_MQTT_PORT", "1883"))

        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.connect_async(broker, port)
        client.loop_start()
        self._client = client
        LOGGER.info("MQTT client started for broker=%s port=%s", broker, port)

    def stop(self) -> None:
        if self._client is None:
            return
        self._client.loop_stop()
        self._client.disconnect()
        self._client = None
        self._connected = False
        self._file_num = 0
        self._data_file = None

    def publish_device_commands(self, commands: list[dict]) -> None:
        if not self._can_publish():
            LOGGER.warning("MQTT unavailable, skipping device command publish commands_count=%s", len(commands))
            return

        for command in commands:
            #payload = {"source": "novaOps", "command": command}
            payload = command
            self._publish_json(self._command_topic, payload, label="device-command")

    def publish_data_saving(self, enabled: bool) -> None:
        if not self._can_publish():
            LOGGER.warning("MQTT unavailable, skipping data-saving publish enabled=%s", enabled)
            return
        
        payload: dict[str, object] = {
            "type": "logger",
            "action": "start_logging" if enabled else "stop_logging",
        }

        if enabled:
            payload["filename"] = self._new_data_file()

        self._publish_json(self._command_topic, payload, label="data-saving")
        if self._control_topic != self._command_topic:
            self._publish_json(self._control_topic, payload, label="data-saving-control")

    def _can_publish(self) -> bool:
        return self._client is not None and self._connected


    def _new_data_file(self) -> str:
        date = datetime.now().strftime("%Y-%m-%d-%H")
        self._data_file = f"{DATA_PATH}/{date}_data_{self._file_num}.csv"
        self._file_num += 1
        return self._data_file


    @staticmethod
    def _connect_success(reason_code) -> bool:
        # paho may pass int, enum-like, or ReasonCode depending on version/callback API.
        try:
            return reason_code == 0
        except Exception:  # noqa: BLE001
            pass

        is_failure = getattr(reason_code, "is_failure", None)
        if is_failure is not None:
            try:
                return not bool(is_failure)
            except Exception:  # noqa: BLE001
                pass

        value = getattr(reason_code, "value", None)
        if value is not None:
            try:
                return int(value) == 0
            except Exception:  # noqa: BLE001
                pass

        return False

    def _publish_json(self, topic: str, payload: dict, label: str) -> None:
        if self._client is None:
            return

        info = self._client.publish(topic, json.dumps(payload), qos=1)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            LOGGER.error("MQTT publish failed label=%s topic=%s rc=%s", label, topic, info.rc)
            return

        LOGGER.info("MQTT publish ok label=%s topic=%s payload=%s", label, topic, payload)

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties) -> None:
        self._connected = self._connect_success(reason_code)
        if not self._connected:
            LOGGER.error("MQTT connect failed reason_code=%s", reason_code)
            return

        client.subscribe(self._sensor_topic)
        LOGGER.info("Subscribed sensor topic topic=%s", self._sensor_topic)

    def _on_disconnect(self, _client, _userdata, _disconnect_flags, reason_code, _properties) -> None:
        self._connected = False
        LOGGER.warning("MQTT disconnected reason_code=%s", reason_code)

    def _on_message(self, _client, _userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            self._on_sensor_message(payload)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Failed to process MQTT message: %s", exc)


def parse_websocket_command(data: dict) -> CommandPayload:
    return CommandPayload.model_validate(data)
