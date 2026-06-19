from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from app.models import (
    ActuatorActions,
    ActuatorEntry,
    ActuatorType,
    CommandPayload,
    SourceTarget,
    SystemCommandPayload,
    SystemConfig,
)

if TYPE_CHECKING:
    from app.context import AppContext


class CommandParser:
    RELAY_TYPES = {ActuatorType.SOLENOID, ActuatorType.POWERED_DEVICE, ActuatorType.POWERED_GPIO_DEVICE}

    def __init__(self, config: SystemConfig) -> None:
        self._config = config

    @staticmethod
    def _is_on_state(state: str) -> bool:
        normalized = state.strip().lower()
        return normalized in {"on", "open", "1", "true", "fill", "armed", "arm", "position_1", "position_2", "position_3"}

    @staticmethod
    def _resolve_power(state: str, actions: ActuatorActions) -> bool:
        """Return True when the underlying relay/device should be energized."""
        state_lower = state.strip().lower()
        solenoid_type = (actions.solenoid_type or "nominally_open").lower()
        if state_lower in {"open", "closed"}:
            return (
                (state_lower == "open" and solenoid_type == "nominally_closed")
                or (state_lower == "closed" and solenoid_type == "nominally_open")
            )
        return CommandParser._is_on_state(state_lower)

    @staticmethod
    def _resolve_relay_state(state: str, relay_type: str | None, solenoid_type: str | None) -> int:
        state_lower = state.strip().lower()
        relay = (relay_type or "nominally_off").lower()
        solenoid = (solenoid_type or "nominally_open").lower()

        if state_lower in {"open", "closed"}:
            power_on = (
                (state_lower == "open" and solenoid == "nominally_closed")
                or (state_lower == "closed" and solenoid == "nominally_open")
            )
        else:
            power_on = CommandParser._is_on_state(state_lower)

        return 0 if ((power_on and relay == "nominally_off") or ((not power_on) and relay == "nominally_on")) else 1

    @staticmethod
    def _resolve_gpio_state(state: str) -> int:
        return 1 if CommandParser._is_on_state(state) else 0

    @staticmethod
    def _resolve_gpio_channel(binding) -> int | None:
        return binding.gpio_channel if binding.gpio_channel is not None else binding.relay_channel

    def _find_actuator(self, name: str) -> ActuatorEntry:
        actuator = self._config.find_actuator(name)
        if actuator is None:
            raise ValueError(f"Actuator '{name}' not found in config")
        return actuator

    def parse(self, payload: CommandPayload) -> list[dict]:
        actuator = self._find_actuator(payload.name)
        if actuator.binding.target == SourceTarget.GCS:
            return self._parse_gcs(actuator, payload.state)
        return self._parse_fas(actuator, payload.state)

    def _parse_gcs(self, actuator: ActuatorEntry, state: str) -> list[dict]:
        binding = actuator.binding
        actions = actuator.actions
        state = state.strip()
        state_lower = state.lower()
        gpio_channel = self._resolve_gpio_channel(binding)

        if actuator.type == ActuatorType.GPIO_DEVICE:
            if gpio_channel is None:
                raise ValueError(f"GPIO actuator '{actuator.name}' does not define gpio_channel")
            return [
                {
                    "type": "gpio",
                    "id": gpio_channel,
                    "state": self._resolve_gpio_state(state),
                }
            ]

        if actuator.type == ActuatorType.POWERED_GPIO_DEVICE:
            if state_lower in {"on", "off"}:
                relay_state = self._resolve_relay_state(state, actions.relay_type, None)
                if binding.relay_channel is None:
                    raise ValueError(f"GPIO actuator '{actuator.name}' does not define relay_channel")
                return [{"type": "relay", "id": binding.relay_channel, "state": relay_state}]
            if gpio_channel is None:
                raise ValueError(f"GPIO actuator '{actuator.name}' does not define gpio_channel")
            return [{"type": "gpio", "id": gpio_channel, "state": self._resolve_gpio_state(state)}]

        if actuator.type in self.RELAY_TYPES:
            relay_state = self._resolve_relay_state(state, actions.relay_type, actions.solenoid_type)
            return [{"type": "relay", "id": binding.relay_channel, "state": relay_state}]

        if actuator.type == ActuatorType.SERVO:
            if state_lower in {"enable", "disable"}:
                angle_state = "on" if state_lower == "enable" else "off"
                return [{"type": "servo", "id": binding.servo_channel, "angle": angle_state}]
            if state_lower in {"on", "off"}:
                if binding.relay_channel is None:
                    raise ValueError(
                        f"Servo '{actuator.name}' does not define relay_channel, so state '{state}' is invalid"
                    )
                relay_state = self._resolve_relay_state(state_lower, actions.relay_type, None)
                return [{"type": "relay", "id": int(binding.relay_channel), "state": relay_state}]

            alias_lookup = {alias.lower(): pos for alias, pos in zip(actions.position_aliases, actions.positions)}
            alias_lookup.update(
                {
                    "position_1": actions.positions[0] if len(actions.positions) > 0 else None,
                    "position_2": actions.positions[1] if len(actions.positions) > 1 else None,
                    "position_3": actions.positions[2] if len(actions.positions) > 2 else None,
                    "open": actions.positions[0] if actions.positions else None,
                    "closed": actions.positions[-1] if actions.positions else None,
                }
            )

            angle = alias_lookup.get(state_lower)
            if angle is None:
                raise ValueError(f"Unsupported servo state '{state}' for actuator '{actuator.name}'")
            return [{"type": "servo", "id": binding.servo_channel, "angle": int(angle)}]

        raise ValueError(f"Unsupported actuator type: {actuator.type}")

    @staticmethod
    def _board_fields(binding: ActuatorBinding) -> dict:
        """Return board_type / board_id fields for a FAS command envelope."""
        if binding.board_type is not None:
            return {"board_type": binding.board_type, "board_id": binding.board_id}
        if binding.node:
            parts = binding.node.rsplit("_", 1)
            if len(parts) == 2 and parts[1].isdigit():
                return {"board_type": parts[0], "board_id": int(parts[1])}
        return {"board_type": None, "board_id": binding.board_id}

    def _parse_fas(self, actuator: ActuatorEntry, state: str) -> list[dict]:
        binding = actuator.binding
        actions = actuator.actions
        state = state.strip()
        gpio_channel = self._resolve_gpio_channel(binding)
        board = self._board_fields(binding)

        def fas(**fields) -> dict:
            return {"type": "fas", **board, **fields}

        if actuator.type == ActuatorType.SERVO:
            state_lower = state.lower()

            if state_lower == "enable":
                return []  # servo enable is a no-op at the wire level

            if state_lower == "disable":
                # Drive PWM to 0 µs to park / disable the servo output.
                return [fas(port="servo", channel=binding.servo_channel, value=0)]

            if state_lower in {"on", "off"}:
                if binding.relay_channel is None:
                    raise ValueError(
                        f"Servo '{actuator.name}' does not define relay_channel, so state '{state}' is invalid"
                    )
                return [fas(port="relay", channel=binding.relay_channel, action=state_lower)]

            alias_lookup = {alias.lower(): pos for alias, pos in zip(actions.position_aliases, actions.positions)}
            micros = alias_lookup.get(state_lower)
            if micros is None:
                raise ValueError(f"Unsupported servo state '{state}' for actuator '{actuator.name}'")
            commands: list[dict] = []
            # FAS servos carry both a power relay and a PWM channel: energise relay then move.
            if binding.relay_channel is not None:
                commands.append(fas(port="relay", channel=binding.relay_channel, action="on"))
            commands.append(fas(port="servo", channel=binding.servo_channel, action=state, value=int(micros)))
            return commands

        if actuator.type in (ActuatorType.SOLENOID, ActuatorType.POWERED_DEVICE):
            power = self._resolve_power(state, actions)
            return [fas(port="relay", channel=binding.relay_channel, action="on" if power else "off")]

        if actuator.type == ActuatorType.POWERED_GPIO_DEVICE:
            if state.lower() in {"on", "off"}:
                if binding.relay_channel is None:
                    raise ValueError(f"GPIO actuator '{actuator.name}' does not define relay_channel")
                return [fas(port="relay", channel=binding.relay_channel, action=state.lower())]
            if gpio_channel is None:
                raise ValueError(f"GPIO actuator '{actuator.name}' does not define gpio_channel")
            return [fas(port="gpio", channel=gpio_channel, action=state)]

        if actuator.type == ActuatorType.GPIO_DEVICE:
            if gpio_channel is None:
                raise ValueError(f"GPIO actuator '{actuator.name}' does not define gpio_channel")
            return [fas(port="gpio", channel=gpio_channel, action=state)]

        raise ValueError(f"Unsupported actuator type: {actuator.type}")


class CommandService:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    async def apply_command(self, command: CommandPayload) -> list[dict]:
        ctx = self._ctx
        if ctx.runtime.lockout_is_locked and ctx.config_service.config.is_hazardous_command(command.name, command.state):
            raise ValueError("Nova is locked; actuator commands are disabled")

        parsed = CommandParser(ctx.config_service.config).parse(command)
        ctx.mqtt_service.publish_device_commands(parsed)

        actuator = ctx.config_service.config.find_actuator(command.name)
        if actuator is not None:
            ctx.runtime.update_actuator_state(actuator, command.state)
        else:
            ctx.runtime.actuator_states[command.name] = {"state": command.state}

        await ctx.broadcast(
            {"type": "actuator_states", "actuator_states": ctx.runtime.actuator_states}
        )
        return parsed

    def apply_system_command(self, payload: SystemCommandPayload) -> dict:
        ctx = self._ctx
        command = ctx.config_service.config.find_command(payload.name)
        if command is None:
            raise ValueError(f"System command '{payload.name}' not found in config")

        if payload.name in {"START_DATA_SAVING", "STOP_DATA_SAVING"}:
            enabled = payload.name == "START_DATA_SAVING"
            ctx.runtime.data_saving_enabled = enabled
            ctx.mqtt_service.publish_data_saving(enabled)
            return {"data_saving_enabled": enabled}

        if payload.name == "GET_DATA_FILES":
            return {"data_files": sorted(path.name for path in ctx.data_dir.glob("*.csv"))}

        raise ValueError(f"Unknown system command: {payload.name!r}")
