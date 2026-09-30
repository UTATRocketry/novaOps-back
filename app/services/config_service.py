from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import yaml

from app.models import FasSensorBinding, SourceTarget, SystemConfig
from app.services.config_history import ConfigHistory

# (file name, content hash, source) - source is one of
# "startup", "load", "upload", "edit".
ConfigListener = Callable[[str, "str | None", str], None]


class ConfigService:
    def __init__(self, config_path: Path, history: ConfigHistory | None = None) -> None:
        self._config_path = config_path
        self._config: SystemConfig | None = None
        self._history = history or ConfigHistory(None)
        self._listeners: list[ConfigListener] = []

    def add_listener(self, listener: ConfigListener) -> None:
        self._listeners.append(listener)

    def record_active(self, source: str, client: str | None = None) -> str | None:
        """Snapshot the active config file into history and notify listeners."""
        digest = self._history.snapshot(self._config_path, source, client)
        for listener in self._listeners:
            listener(self._config_path.name, digest, source)
        return digest

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

    def set_config_path(self, config_path: Path, source: str = "load", client: str | None = None) -> SystemConfig:
        self._config_path = config_path
        config = self.reload()
        self.record_active(source, client)
        return config

    def update_config(self, config_data: dict[str, Any], client: str | None = None) -> SystemConfig:
        config = self._parse_config(config_data)
        self._check_duplicates(config)
        # Persist the parsed model (not the raw payload) so the written file stays
        # compact: defaults and null fields are dropped instead of being echoed back.
        clean_data = config.model_dump(
            by_alias=True,
            mode="json",
            exclude_defaults=True,
            exclude_none=True,
        )
        self._write_yaml(self._config_path, clean_data)
        self._config = config
        self.record_active("edit", client)
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
                address = ("FAS", binding.resolved_node, binding.channel)
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
        path.write_text(
            yaml.dump(data, Dumper=_ConfigDumper, sort_keys=False),
            encoding="utf-8",
        )


class _ConfigDumper(yaml.SafeDumper):
    """SafeDumper that renders scalar leaf-lists inline (e.g. ``range: [0, 1000]``)
    while keeping lists of mappings/nested lists in readable block style."""


def _represent_list(dumper: yaml.Dumper, data: list[Any]) -> Any:
    all_scalar = all(isinstance(item, (int, float, str, bool)) or item is None for item in data)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=all_scalar)


_ConfigDumper.add_representer(list, _represent_list)
