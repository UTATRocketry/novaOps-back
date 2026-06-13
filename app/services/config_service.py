from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.models import FasSensorBinding, SourceTarget, SystemConfig


class ConfigService:
    def __init__(self, config_path: Path) -> None:
        self._config_path = config_path
        self._config: SystemConfig | None = None

    @property
    def config(self) -> SystemConfig:
        if self._config is None:
            self.reload()
        return self._config

    @property
    def config_path(self) -> Path:
        return self._config_path

    def reload(self) -> SystemConfig:
        data = self._read_yaml(self._config_path)
        config = self._parse_config(data)
        self._check_duplicates(config)
        self._config = config
        return self._config

    def set_config_path(self, config_path: Path) -> SystemConfig:
        self._config_path = config_path
        return self.reload()

    def update_config(self, config_data: dict[str, Any]) -> SystemConfig:
        config = self._parse_config(config_data)
        self._check_duplicates(config)
        self._write_yaml(self._config_path, config_data)
        self._config = config
        return config

    def upload_config_bytes(self, payload: bytes) -> SystemConfig:
        config_data = yaml.safe_load(payload.decode("utf-8")) or {}
        return self.update_config(config_data)

    @staticmethod
    def _parse_config(config_data: dict[str, Any]) -> SystemConfig:
        return SystemConfig.model_validate(config_data)

    @staticmethod
    def _check_duplicates(config: SystemConfig) -> None:
        """Fail loudly on config typos that would otherwise mis-route silently."""
        sensor_addresses: dict[tuple, str] = {}
        for sensor in config.sensors:
            binding = sensor.binding
            if isinstance(binding, FasSensorBinding):
                address = ("FAS", binding.node, binding.channel)
            else:
                address = (binding.source, binding.hat_id, binding.channel_id)
            if address in sensor_addresses:
                raise ValueError(
                    f"Duplicate sensor address {address}: '{sensor_addresses[address]}' and '{sensor.name}'"
                )
            sensor_addresses[address] = sensor.name

        actuator_names: set[str] = set()
        for actuator in config.actuators:
            if actuator.name in actuator_names:
                raise ValueError(f"Duplicate actuator name '{actuator.name}'")
            actuator_names.add(actuator.name)

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    @staticmethod
    def _write_yaml(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
