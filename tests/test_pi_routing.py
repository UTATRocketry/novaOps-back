"""Per-Pi command topics (docs/design/pi-io-config.md, "Command topics")."""
from __future__ import annotations

import asyncio

import pytest

from app.context import AppContext
from app.models import (
    ActuatorEntry,
    CommandPayload,
    DirectRelayPayload,
    DirectServoPayload,
    SourceTarget,
    SystemConfig,
)
from app.services.command_service import CommandParser
from app.services.mqtt_service import MqttService

CONFIG = """
Actuators:
  - name: GCS_SOL
    type: solenoid
    binding: {target: GCS, relay_channel: 0}
    actions: {relay_type: nominally_off, solenoid_type: nominally_closed}
  - name: GCS_SERVO
    type: servo
    binding: {target: GCS, relay_channel: 1, servo_channel: 1}
    actions: {position_aliases: [open, closed], positions: [2000, 1000]}
  - name: GCS_GPIO
    type: gpio_device
    binding: {target: GCS, gpio_channel: 22}
  - name: TCS_HTR
    type: gpio_device
    binding: {target: TCS, gpio_channel: 17}
  - name: TCS_RELAY
    type: powered_device
    binding: {target: TCS, relay_channel: 3}
    actions: {relay_type: nominally_off}
  - name: TCS_PGPIO
    type: powered_gpio_device
    binding: {target: TCS, relay_channel: 4, gpio_channel: 27}
  - name: FAS_SOL
    type: solenoid
    binding: {target: FAS, node: EPB_0, relay_channel: 3}
    actions: {solenoid_type: nominally_closed}
"""


def _config() -> SystemConfig:
    import yaml

    return SystemConfig.model_validate(yaml.safe_load(CONFIG))


def _route(name: str, state: str):
    return CommandParser(_config()).route(CommandPayload(type="x", name=name, state=state))


@pytest.mark.parametrize(
    ("name", "state", "topic", "commands"),
    [
        ("GCS_SOL", "open", "nova/command/novaGround", [{"type": "relay", "id": 0, "state": 0}]),
        ("GCS_SERVO", "open", "nova/command/novaGround", [{"type": "servo", "id": 1, "angle": 2000}]),
        ("GCS_GPIO", "arm", "nova/command/novaGround", [{"type": "gpio", "id": 22, "state": 1}]),
        ("TCS_HTR", "arm", "nova/command/novaThermo", [{"type": "gpio", "id": 17, "state": 1}]),
        ("TCS_HTR", "disarm", "nova/command/novaThermo", [{"type": "gpio", "id": 17, "state": 0}]),
        ("TCS_RELAY", "on", "nova/command/novaThermo", [{"type": "relay", "id": 3, "state": 0}]),
        ("TCS_PGPIO", "off", "nova/command/novaThermo", [{"type": "relay", "id": 4, "state": 1}]),
        ("TCS_PGPIO", "arm", "nova/command/novaThermo", [{"type": "gpio", "id": 27, "state": 1}]),
        ("FAS_SOL", "open", "nova/command",
         [{"type": "fas", "board_type": "EPB", "board_id": 0, "port": "relay", "channel": 3, "action": "on"}]),
    ],
)
def test_every_target_routes_to_its_own_topic(name, state, topic, commands) -> None:
    assert _route(name, state) == (topic, commands)


def test_tcs_never_reaches_the_fas_path() -> None:
    # Before #40 a TCS gpio_device "arm" became IGN_ARM on EPB 0.
    for name in ("TCS_HTR", "TCS_RELAY", "TCS_PGPIO"):
        for state in ("arm", "on", "off", "disarm"):
            try:
                topic, commands = _route(name, state)
            except ValueError:
                continue
            assert topic == "nova/command/novaThermo"
            assert all(cmd["type"] != "fas" for cmd in commands)


def test_parse_still_returns_just_the_commands() -> None:
    parser = CommandParser(_config())
    assert parser.parse(CommandPayload(type="x", name="TCS_HTR", state="arm")) == [
        {"type": "gpio", "id": 17, "state": 1}
    ]


def test_ops_actuator_is_rejected_by_validation() -> None:
    with pytest.raises(ValueError, match="no command path"):
        SystemConfig.model_validate(
            {"Actuators": [{"name": "X", "type": "solenoid", "binding": {"target": "OPS", "relay_channel": 0}}]}
        )


def test_ops_actuator_that_slips_past_validation_is_not_sent() -> None:
    actuator = ActuatorEntry.model_validate(
        {"name": "X", "type": "solenoid", "binding": {"target": "GCS", "relay_channel": 0}}
    )
    actuator.binding.target = SourceTarget.OPS  # assignment isn't validated
    with pytest.raises(ValueError, match="no command path"):
        CommandParser(SystemConfig()).route_actuator(actuator, "open")


# --- through the service: topic reaches MQTT, broadcasts stay broadcast ---

class _Info:
    rc = 0

    def wait_for_publish(self, _timeout=None):
        return True


class _Client:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, int, bool]] = []

    def publish(self, topic, payload, qos=1, retain=False):
        self.published.append((topic, payload, qos, retain))
        return _Info()

    def subscribe(self, topic):
        pass


@pytest.fixture
def ctx(tmp_path):
    path = tmp_path / "system.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    context = AppContext(path, tmp_path / "data")
    context.runtime.initialize_all(context.config_service.config)
    context.runtime.lockout_state = "unlocked"
    client = _Client()
    context.mqtt_service._client = client
    context.mqtt_service._connected = True

    async def no_broadcast(_payload):
        return None

    context.broadcast = no_broadcast  # type: ignore[method-assign]
    return context, client


def _topics(client: _Client) -> list[str]:
    return [topic for topic, *_ in client.published]


@pytest.mark.parametrize(
    ("name", "state", "topic"),
    [
        ("GCS_SOL", "open", "nova/command/novaGround"),
        ("TCS_HTR", "arm", "nova/command/novaThermo"),
        ("FAS_SOL", "open", "nova/command"),
    ],
)
def test_apply_command_publishes_on_the_targets_topic(ctx, name, state, topic) -> None:
    context, client = ctx
    asyncio.run(context.command_service.apply_command(CommandPayload(type="x", name=name, state=state)))
    assert _topics(client) == [topic]


def test_direct_relay_and_servo_route_by_target(ctx) -> None:
    context, client = ctx
    context.command_service.apply_direct_relay(DirectRelayPayload(target="GCS", channel=2, state=1))
    context.command_service.apply_direct_servo(DirectServoPayload(target="GCS", channel=1, pulse_us=1500))
    context.command_service.apply_direct_relay(DirectRelayPayload(target="FAS", node="EPB_1", channel=2, state=1))
    assert _topics(client) == ["nova/command/novaGround", "nova/command/novaGround", "nova/command"]


def test_data_saving_is_still_a_broadcast(ctx) -> None:
    context, client = ctx
    context.set_data_saving(True)
    context.set_data_saving(False)
    data_file = [t for t, payload, *_ in client.published if '"data_file"' in payload]
    assert data_file and set(data_file) <= {"nova/command", "nova/control"}
    assert "nova/command" in data_file


def test_console_commands_are_still_a_broadcast(ctx) -> None:
    context, client = ctx
    context.mqtt_service.publish_console_command({"action": "status"})
    assert _topics(client) == ["nova/command"]


def test_publish_without_a_topic_uses_the_broadcast_topic() -> None:
    svc = MqttService("s", "nova/command", "nova/control", on_sensor_message=lambda _: None)
    client = _Client()
    svc._client, svc._connected = client, True
    svc.publish_device_commands([{"type": "relay", "id": 1, "state": 1}])
    svc.publish_device_commands([{"type": "relay", "id": 1, "state": 1}], topic="nova/command/novaThermo")
    assert _topics(client) == ["nova/command", "nova/command/novaThermo"]
