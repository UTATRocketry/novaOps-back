"""The per-Pi config message and the Pi's config status.

See docs/design/pi-io-config.md ("The per-node config message", "Output
limits"). The backend publishes a retained ``nova/config/<node>`` for each Pi
(novaGround for GCS, novaThermo for TCS) on connect and on every active-config
change. Each Pi answers on a retained ``nova/config/<node>/status`` with what it
applied, what it rejected and which outputs it tripped.

A trip is applied to actuator state with the same rules commands use, by
finding the command state that puts the tripped output at its safe level. The
backend never sends a command because of a trip: that would defeat the limit.
"""
from __future__ import annotations

import logging
from typing import Any

from app.models import (
    PI_NODES,
    PI_RESERVED_GPIO,
    ActuatorEntry,
    ActuatorType,
    GcsSensorBinding,
    PiGpioSensorBinding,
    SensorType,
    SourceTarget,
    SystemConfig,
)

LOGGER = logging.getLogger(__name__)

CONFIG_TOPIC_PREFIX = "nova/config"
CONFIG_STATUS_FILTER = "nova/config/+/status"
HEARTBEAT_TOPIC = "nova/heartbeat/backend"
HEARTBEAT_INTERVAL_S = 0.25

# node name -> target, the reverse of PI_NODES.
TARGET_FOR_NODE = {node: target for target, node in PI_NODES.items()}

# The level each kind of output returns to when it trips. novaGround's relay
# board is active-low: startup writes 1 to every relay, which de-energizes it.
SAFE_LEVEL = {"gpio": 0, "relay": 1}

TRIP_KEYS = ("tripped", "tripped_output", "tripped_ms")


def config_topic(node: str) -> str:
    return f"{CONFIG_TOPIC_PREFIX}/{node}"


def node_from_status_topic(topic: str) -> str | None:
    parts = topic.split("/")
    if len(parts) == 4 and parts[0] == "nova" and parts[1] == "config" and parts[3] == "status" and parts[2]:
        return parts[2]
    return None


def _merge_limits(existing: dict | None, limits: dict) -> dict:
    """Two actuators on one output: the smallest max on-time and `safe` win."""
    if existing is None:
        return dict(limits)
    times = [t for t in (existing["max_on_ms"], limits["max_on_ms"]) if t is not None]
    return {
        "max_on_ms": min(times) if times else None,
        "on_comms_loss": "safe" if "safe" in (existing["on_comms_loss"], limits["on_comms_loss"]) else "hold",
    }


def _limits(actuator: ActuatorEntry) -> dict:
    max_on_s = actuator.actions.max_on_s
    return {
        "max_on_ms": None if max_on_s is None else int(round(max_on_s * 1000)),
        "on_comms_loss": actuator.actions.on_comms_loss,
    }


def _sorted_by_int(mapping: dict[str, Any]) -> dict[str, Any]:
    return {key: mapping[key] for key in sorted(mapping, key=int)}


def build_node_config(config: SystemConfig, target: SourceTarget, config_hash: str | None) -> dict:
    """The retained nova/config/<node> message for one Pi target."""
    outputs: dict[str, dict] = {}
    relays: dict[str, dict] = {}
    for actuator in config.actuators:
        if actuator.binding.target != target:
            continue
        limits = _limits(actuator)
        pin = actuator.pi_gpio_output
        if pin is not None:
            outputs[str(pin)] = _merge_limits(outputs.get(str(pin)), limits)
        if actuator.actions.has_output_limits:
            # Only relay channels with limits are listed; the rest behave as today.
            for channel in actuator.relay_outputs:
                relays[str(channel)] = _merge_limits(relays.get(str(channel)), limits)

    inputs = sorted({
        sensor.binding.gpio_channel for sensor in config.sensors
        if isinstance(sensor.binding, PiGpioSensorBinding) and sensor.binding.source == target
    })

    daq: dict[str, dict[str, str]] = {}
    for sensor in config.sensors:
        binding = sensor.binding
        if sensor.type != SensorType.TC or not isinstance(binding, GcsSensorBinding):
            continue
        if binding.source != target:
            continue
        daq.setdefault(str(binding.hat_id), {})[str(binding.channel_id)] = sensor.effective_tc_type

    return {
        "config_hash": config_hash,
        "gpio": {"outputs": _sorted_by_int(outputs), "inputs": inputs},
        "relays": _sorted_by_int(relays),
        "daq": {hat: _sorted_by_int(channels) for hat, channels in _sorted_by_int(daq).items()},
        "heartbeat": {"timeout_ms": config.heartbeat.timeout_ms},
    }


def build_all_node_configs(config: SystemConfig, config_hash: str | None) -> dict[str, dict]:
    return {node: build_node_config(config, target, config_hash) for target, node in PI_NODES.items()}


# --- status ---------------------------------------------------------------

def parse_output(output: Any) -> tuple[str, int] | None:
    """'gpio:17' -> ('gpio', 17); 'relay:3' -> ('relay', 3); anything else None."""
    if not isinstance(output, str) or ":" not in output:
        return None
    kind, _, number = output.partition(":")
    kind = kind.strip().lower()
    if kind not in SAFE_LEVEL:
        return None
    try:
        return kind, int(number)
    except ValueError:
        return None


def actuator_outputs(actuator: ActuatorEntry) -> set[tuple[str, int]]:
    outputs = {("relay", channel) for channel in actuator.relay_outputs}
    if actuator.pi_gpio_output is not None:
        outputs.add(("gpio", actuator.pi_gpio_output))
    return outputs


def actuators_for_output(config: SystemConfig, target: SourceTarget, output: tuple[str, int]) -> list[ActuatorEntry]:
    return [actuator for actuator in config.actuators
            if actuator.binding.target == target and output in actuator_outputs(actuator)]


def _trips(status: dict | None) -> list[dict]:
    if not isinstance(status, dict):
        return []
    tripped = status.get("tripped")
    return [trip for trip in tripped if isinstance(trip, dict)] if isinstance(tripped, list) else []


def status_view(node: str, status: dict | None, config: SystemConfig, expected_hash: str | None,
                received_ms: int | None = None, published_ms: int | None = None) -> dict:
    """What the frontend gets for one Pi: its status plus whether it matches.

    `received_ms` is when the backend last got this Pi's status, and
    `published_ms` when it first sent the Pi the active config (`expected_hash`),
    both backend wall-clock milliseconds. A Pi whose `config_hash` differs and
    that rejected nothing hasn't applied the config yet: it is `pending` since
    `published_ms`.
    """
    target = TARGET_FOR_NODE.get(node)
    received = isinstance(status, dict)
    status = status if received else {}
    rejected = status.get("rejected") if isinstance(status.get("rejected"), list) else []
    reserved = status.get("reserved_pins") if isinstance(status.get("reserved_pins"), list) else []
    thermocouples = status.get("thermocouples") if isinstance(status.get("thermocouples"), dict) else {}

    tripped = []
    for trip in _trips(status):
        output = parse_output(trip.get("output"))
        names = [a.name for a in actuators_for_output(config, target, output)] if output and target else []
        tripped.append({
            "output": trip.get("output"),
            "reason": trip.get("reason"),
            "time_ms": trip.get("time_ms"),
            "actuators": names,
        })

    reasons = []
    if received:
        if status.get("config_hash") != expected_hash:
            reasons.append("config_hash")
        if rejected:
            reasons.append("rejected")
    if not received:
        apply_state = "waiting"
    elif rejected:
        apply_state = "rejected"
    elif status.get("config_hash") != expected_hash:
        apply_state = "pending"
    else:
        apply_state = "applied"
    return {
        "node": node,
        "target": target.value if target else None,
        "expected_hash": expected_hash,
        "received": received,
        "config_hash": status.get("config_hash"),
        "applied_ms": status.get("applied_ms"),
        "rejected": rejected,
        "reserved_pins": reserved,
        "thermocouples": thermocouples,
        "tripped": tripped,
        "mismatch": bool(reasons),
        "mismatch_reasons": reasons,
        # waiting: no status yet; applied: the active config, nothing rejected;
        # pending: the Pi still holds another config; rejected: it refused items.
        "apply_state": apply_state,
        "received_ms": received_ms if received else None,
        "published_ms": published_ms,
    }


def _candidate_states(actuator: ActuatorEntry) -> list[str]:
    if actuator.type == ActuatorType.SOLENOID:
        return ["closed", "open"]
    if actuator.type in (ActuatorType.POWERED_DEVICE, ActuatorType.SERVO):
        # A servo's only relay output is its power relay, switched by on/off.
        return ["off", "on"]
    if actuator.type == ActuatorType.POWERED_GPIO_DEVICE:
        return ["off", "on", "disarm", "arm"]
    if actuator.type == ActuatorType.GPIO_DEVICE:
        return ["disarm", "arm"]
    if actuator.type == ActuatorType.MOTOR:
        neutral = actuator.motor_neutral_label
        return [neutral] + [label for label in actuator.motor_labels if label != neutral]
    return []


def _wire_levels(commands: list[dict]) -> dict[tuple[str, int], int]:
    return {(cmd["type"], int(cmd["id"])): int(cmd["state"])
            for cmd in commands if cmd.get("type") in SAFE_LEVEL and cmd.get("id") is not None}


def safe_state(config: SystemConfig, actuator: ActuatorEntry, tripped: set[tuple[str, int]],
               current: dict | None) -> str | None:
    """The command state that leaves `tripped` outputs at their safe level.

    Uses the command parser, so relay_type, solenoid_type, motor patterns and
    invert_relays apply exactly as for a command. For a motor, its other relay
    must stay at the level the current state holds it at.
    """
    from app.services.command_service import CommandParser

    parser = CommandParser(config)
    mine = tripped & actuator_outputs(actuator)
    if not mine:
        return None
    current_levels: dict[tuple[str, int], int] = {}
    current_label = (current or {}).get("motion") if actuator.type == ActuatorType.MOTOR else None
    if current_label:
        try:
            current_levels = _wire_levels(parser.route_actuator(actuator, current_label)[1])
        except (ValueError, KeyError, TypeError):
            current_levels = {}

    for candidate in _candidate_states(actuator):
        try:
            levels = _wire_levels(parser.route_actuator(actuator, candidate)[1])
        except (ValueError, KeyError, TypeError):
            continue
        touched = set(levels) & mine
        if not touched or any(levels[o] != SAFE_LEVEL[o[0]] for o in touched):
            continue
        if any(o not in mine and o in current_levels and current_levels[o] != level
               for o, level in levels.items()):
            continue
        return candidate
    return None


def apply_trips(config: SystemConfig, statuses: dict[str, dict], actuator_states: dict[str, dict],
                applied: set[tuple], update_state) -> bool:
    """Apply new trips from every Pi's status to actuator state.

    `applied` remembers (node, output, time_ms) so a retained status that is
    delivered again doesn't re-apply an old trip after a later command.
    `update_state(actuator, state)` is RuntimeState.update_actuator_state.
    Returns True if any actuator state changed.
    """
    changed = False
    still_tripped: set[str] = set()
    for node, status in statuses.items():
        target = TARGET_FOR_NODE.get(node)
        if target is None:
            continue
        for trip in _trips(status):
            output = parse_output(trip.get("output"))
            if output is None:
                LOGGER.warning("Ignoring trip with unknown output %r from %s", trip.get("output"), node)
                continue
            for actuator in actuators_for_output(config, target, output):
                still_tripped.add(actuator.name)
                key = (node, trip.get("output"), trip.get("time_ms"))
                if (key, actuator.name) in applied:
                    continue
                applied.add((key, actuator.name))
                state = safe_state(config, actuator, {output}, actuator_states.get(actuator.name))
                if state is not None:
                    update_state(actuator, state)
                else:
                    LOGGER.warning("No state of %s matches tripped output %s", actuator.name, trip.get("output"))
                entry = actuator_states.setdefault(actuator.name, {})
                entry["tripped"] = str(trip.get("reason") or "tripped")
                entry["tripped_output"] = str(trip.get("output"))
                entry["tripped_ms"] = trip.get("time_ms")
                LOGGER.warning("%s tripped on %s (%s), state now %s",
                               actuator.name, node, trip.get("reason"), state)
                changed = True

    # A trip the Pi no longer lists was cleared by a command for that output.
    for name, entry in actuator_states.items():
        if name not in still_tripped and isinstance(entry, dict) and "tripped" in entry:
            for key in TRIP_KEYS:
                entry.pop(key, None)
            changed = True
    return changed


__all__ = [
    "CONFIG_STATUS_FILTER",
    "HEARTBEAT_INTERVAL_S",
    "HEARTBEAT_TOPIC",
    "PI_RESERVED_GPIO",
    "TARGET_FOR_NODE",
    "apply_trips",
    "build_all_node_configs",
    "build_node_config",
    "config_topic",
    "node_from_status_topic",
    "safe_state",
    "status_view",
]
