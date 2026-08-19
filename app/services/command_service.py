from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from app.models import (
    ActuatorBinding,
    ActuatorActions,
    ActuatorEntry,
    ActuatorType,
    CommandPayload,
    DirectRelayPayload,
    DirectServoPayload,
    FasAuxPayload,
    FasBuzzerPayload,
    FasChargerPayload,
    FasRabPayload,
    FasRadioConfigPayload,
    FasRuncamRecordPayload,
    FasSdPayload,
    FasSoundPayload,
    SourceTarget,
    SystemCommandPayload,
    SystemConfig,
)

if TYPE_CHECKING:
    from app.context import AppContext

LOGGER = logging.getLogger(__name__)


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
    def _motor_relay_steps(actuator: ActuatorEntry, state: str) -> list[tuple[int, int]]:
        """Return (relay_channel, relay_state) pairs for a motor state label.

        De-energized legs are emitted first ("break before make") so a reversible
        pair driven through reverse polarity is never closed on both legs, even
        for the instant between the two relay commands.
        """
        pattern = actuator.resolve_motor_state(state)
        steps = sorted(zip(actuator.motor_channels, pattern), key=lambda step: step[1])
        if actuator.actions.invert_relays:
            return [(channel, 1 - value) for channel, value in steps]
        return steps

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

        if actuator.type == ActuatorType.MOTOR:
            return [
                {"type": "relay", "id": channel, "state": relay_state}
                for channel, relay_state in self._motor_relay_steps(actuator, state)
            ]

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
        """Return board_type / board_id fields for a FAS command envelope.

        The node string ("EPB_4") is the binding's canonical address: it is
        what the config editor writes and what sensors already resolve through
        FasSensorBinding.resolved_node. The loose board_type/board_id pair is
        only trusted when no parseable node exists. Preferring the pair broke
        BVFTP: its config carried node="EPB_4" plus a stale board_type="EPB"/
        board_id=0, so every command went on the wire addressed to EPB:0 - a
        board that does not exist - and was silently dropped by every EPB's
        board-id filter, while the same command from the legacy GS (which
        sends an explicit board_id) worked. The bridge then prefers an
        explicit board_id over the node, so the 0 always won downstream too.
        """
        if binding.node:
            parts = binding.node.rsplit("_", 1)
            if len(parts) == 2 and parts[1].isdigit():
                return {"board_type": parts[0], "board_id": int(parts[1])}
        if binding.board_type is not None:
            return {"board_type": binding.board_type, "board_id": binding.board_id}
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

        if actuator.type == ActuatorType.MOTOR:
            return [
                fas(port="relay", channel=channel, action="on" if relay_state else "off")
                for channel, relay_state in self._motor_relay_steps(actuator, state)
            ]

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
        # upload_id -> asyncio.Future resolved with the bridge's ack payload.
        # Written from the request coroutine, resolved from the MQTT thread.
        self._sound_uploads: dict[str, asyncio.Future] = {}
        self._sound_uploads_lock = threading.Lock()

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

    @staticmethod
    def _fas_board(node: str | None) -> dict:
        """Parse a node string like 'EPB_4' into FAS board_type / board_id fields."""
        if node:
            parts = node.rsplit("_", 1)
            if len(parts) == 2 and parts[1].isdigit():
                return {"board_type": parts[0], "board_id": int(parts[1])}
        return {"board_type": None, "board_id": 0}

    def apply_direct_relay(self, payload: DirectRelayPayload) -> list[dict]:
        if payload.target == "GCS":
            commands = [{"type": "relay", "id": payload.channel, "state": payload.state}]
        else:
            board = self._fas_board(payload.node)
            action = "on" if payload.state == 1 else "off"
            commands = [{"type": "fas", **board, "port": "relay", "channel": payload.channel, "action": action}]
        self._ctx.mqtt_service.publish_device_commands(commands)
        return commands

    def apply_direct_servo(self, payload: DirectServoPayload) -> list[dict]:
        if payload.target == "GCS":
            commands = [{"type": "servo", "id": payload.channel, "angle": payload.pulse_us}]
        else:
            board = self._fas_board(payload.node)
            commands = [{"type": "fas", **board, "port": "servo", "channel": payload.channel, "value": payload.pulse_us}]
        self._ctx.mqtt_service.publish_device_commands(commands)
        return commands

    def apply_fas_buzzer(self, payload: FasBuzzerPayload) -> list[dict]:
        board = self._fas_board(payload.node)

        if payload.action == "stop":
            command = {"type": "fas", **board, "op": "buzzer", "action": "stop"}
            self._ctx.mqtt_service.publish_device_commands([command])
            return [command]

        # action == "play": resolve the note list from an explicit array or a
        # named melody in config, then hand the whole melody to the bridge in
        # one command (it streams BEGIN/NOTE.../PLAY to the FMC).
        notes = payload.notes
        if notes is None and payload.melody is not None:
            notes = self._ctx.config_service.config.find_buzzer_melody(payload.melody)
            if notes is None:
                raise ValueError(f"Buzzer melody '{payload.melody}' not found in config")
        if not notes:
            raise ValueError("A buzzer 'play' needs either 'notes' or a known 'melody'")

        command = {"type": "fas", **board, "op": "buzzer", "action": "play", "notes": notes}
        self._ctx.mqtt_service.publish_device_commands([command])
        return [command]

    def apply_fas_sd(self, payload: FasSdPayload) -> list[dict]:
        board = self._fas_board(payload.node)
        command = {
            "type": "fas", **board, "op": "sd_cmd",
            "action": payload.action, "divisor": payload.divisor,
        }
        self._ctx.mqtt_service.publish_device_commands([command])
        return [command]

    def apply_fas_rab(self, payload: FasRabPayload) -> list[dict]:
        """Recovery Arming Board arm/disarm. The RAB is addressed by board_id
        (0 = A, 1 = B); the bridge routes to the RAB board kind on the wire.

        ARMING is hazardous, so it is blocked while Nova is locked (same gate as
        hazardous actuator commands). DISARMING is a safing action and is always
        permitted — the lock must never be able to trap a RAB in the armed state."""
        if payload.action == "arm" and self._ctx.runtime.lockout_is_locked:
            raise ValueError("Nova is locked; RAB arming is disabled")
        op = "rab_arm" if payload.action == "arm" else "rab_disarm"
        command = {
            "type": "fas", "board_type": "RAB", "board_id": payload.rab_id,
            "op": op, "pulse_ms": payload.pulse_ms,
        }
        self._ctx.mqtt_service.publish_device_commands([command])
        return [command]

    def apply_fas_aux(self, payload: FasAuxPayload) -> list[dict]:
        board = self._fas_board(payload.node or "FMC_0")
        command = {
            "type": "fas", **board, "op": "aux_power",
            "device": payload.device, "enable": payload.enable,
            # None means "defer to the FMC's persisted default"; the bridge
            # turns that into the 0xFFFF wire sentinel. Only read for runcam.
            "autostop_s": payload.autostop_s,
        }
        self._ctx.mqtt_service.publish_device_commands([command])
        return [command]

    def apply_fas_runcam_record(self, payload: FasRuncamRecordPayload) -> list[dict]:
        board = self._fas_board(payload.node or "FMC_0")
        command = {
            "type": "fas", **board, "op": "runcam_record",
            "enable": payload.enable, "autostop_s": payload.autostop_s,
        }
        self._ctx.mqtt_service.publish_device_commands([command])
        return [command]

    def apply_fas_radio_config(self, payload: FasRadioConfigPayload) -> list[dict]:
        board = self._fas_board(payload.node or "FMC_0")
        command = {
            "type": "fas", **board, "op": "radio_config",
            "action": payload.action, "transaction_id": payload.transaction_id,
            "cfg": payload.cfg,
        }
        self._ctx.mqtt_service.publish_device_commands([command])
        return [command]

    def apply_fas_sound(self, payload: FasSoundPayload) -> list[dict]:
        board = self._fas_board(payload.node or "FMC_0")
        command = {
            "type": "fas", **board, "op": "sound", "action": payload.action,
            "idx": payload.idx, "volume": payload.volume,
            "freq_hz": payload.freq_hz, "ms": payload.ms,
        }
        self._ctx.mqtt_service.publish_device_commands([command])
        return [command]

    def apply_fas_charger(self, payload: FasChargerPayload) -> list[dict]:
        board = self._fas_board(payload.node or "PMB_0")
        # 0xFF = leave the persisted limit unchanged (matches the firmware).
        command = {
            "type": "fas", **board, "op": "pmb_charger",
            "enable": payload.enable,
            "i_setting": 0xFF if payload.i_setting is None else payload.i_setting,
            "v_setting": 0xFF if payload.v_setting is None else payload.v_setting,
        }
        self._ctx.mqtt_service.publish_device_commands([command])
        return [command]

    async def apply_fas_sound_upload(self, name: str, clip: bytes, fmt: int, crc32: int,
                                     sample_rate: int, node: str | None = None,
                                     base_url: str | None = None,
                                     timeout_s: float | None = None) -> dict:
        """Stage a transcoded soundboard clip and have the bridge fetch it.

        The clip bytes are written to a temp file and the MQTT command carries
        only a one-shot download URL, so clip size is no longer bounded by what
        fits in an MQTT message. The bridge downloads the file, streams it to the
        FMC as BEGIN/DATA/END, and publishes a ``sound_upload_result`` ack on the
        console topic; we wait for that ack (or time out) and return its outcome.
        """
        board = self._fas_board(node or "FMC_0")
        clip_ref = self._ctx.clip_store.stage(
            clip, meta={"name": name, "format": fmt, "crc32": crc32})
        path = f"/api/fas/sound/clip/{clip_ref.token}"
        command = {
            "type": "fas", **board, "op": "sound_upload",
            "upload_id": clip_ref.token,
            "name": name, "format": fmt, "sample_rate": sample_rate,
            "crc32": crc32, "bytes": len(clip),
            # `path` lets a bridge configured with its own --ops-url ignore the
            # backend's guess at its externally reachable address.
            "path": path,
            "url": f"{(base_url or '').rstrip('/')}{path}" if base_url else path,
        }

        if timeout_s is None:
            # The bridge paces DATA frames at ~40 kB/s into the FMC's flash, plus
            # erase/verify round trips at each end. Allow generous slack.
            timeout_s = 45.0 + len(clip) / 20_000.0

        waiter = self._register_sound_upload(clip_ref.token)
        summary = {"upload_id": clip_ref.token, "name": name, "format": fmt,
                   "bytes": len(clip), "crc32": crc32, "sample_rate": sample_rate,
                   "url": command["url"]}
        try:
            self._ctx.mqtt_service.publish_device_commands([command])
            try:
                ack = await asyncio.wait_for(waiter, timeout_s)
            except asyncio.TimeoutError:
                # Leave the staged clip in place: a slow bridge may still collect
                # it, and the store expires it on its own.
                return {**summary, "ok": False, "stage": "timeout",
                        "error": f"No bridge acknowledgement within {timeout_s:.0f}s"}
            self._ctx.clip_store.discard(clip_ref.token)
            return {
                **summary,
                "ok": bool(ack.get("ok")),
                "stage": ack.get("stage"),
                "error": ack.get("error"),
                "clip_count": ack.get("clip_count"),
            }
        finally:
            self._forget_sound_upload(clip_ref.token)

    def _register_sound_upload(self, upload_id: str) -> asyncio.Future:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        with self._sound_uploads_lock:
            self._sound_uploads[upload_id] = future
        return future

    def _forget_sound_upload(self, upload_id: str) -> None:
        with self._sound_uploads_lock:
            self._sound_uploads.pop(upload_id, None)

    def resolve_sound_upload(self, payload: dict) -> None:
        """Resolve a pending upload with the bridge's ack. Called from the MQTT
        thread, so the future is completed on its own event loop."""
        upload_id = str(payload.get("upload_id") or "")
        with self._sound_uploads_lock:
            future = self._sound_uploads.get(upload_id)
        if future is None or future.done():
            return
        loop = future.get_loop()

        def _set() -> None:
            if not future.done():
                future.set_result(payload)

        try:
            loop.call_soon_threadsafe(_set)
        except RuntimeError as exc:  # loop closed mid-shutdown
            LOGGER.warning("Could not deliver sound upload ack %s: %s", upload_id, exc)

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
