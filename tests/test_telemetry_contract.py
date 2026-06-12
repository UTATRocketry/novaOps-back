import pytest

from app.context import AppContext


def _context(tmp_path) -> AppContext:
    config_path = tmp_path / "system.yaml"
    config_path.write_text(
        """
safetyRules:
  hazardous:
    - SVFTV: ALL
Sensors:
  - name: PFT
    type: PT
    unit: psi
    binding:
      source: FAS
      node: EPB_1
      channel: 1
    convert:
      method: none
Actuators:
  - name: SVFTV
    type: solenoid
    binding:
      target: FAS
      node: EPB_1
      relay_channel: 2
    actions:
      relay_type: nominally_off
      solenoid_type: nominally_closed
""",
        encoding="utf-8",
    )
    return AppContext(config_path, tmp_path / "data")


def test_engine_topic_updates_engine_data_and_legacy_sensors(tmp_path) -> None:
    context = _context(tmp_path)

    context._on_engine_message(
        {
            "source": "FAS",
            "sensors": [{"node": "EPB_1", "channel": 1, "value": 12.34, "timestamp": 9}],
        }
    )

    assert context.runtime.latest_engine_data == [
        {"name": "PFT", "value": 12.34, "avg": 12.34, "unit": "psi", "timestamp": 9}
    ]
    assert context.runtime.latest_sensors == context.runtime.latest_engine_data


def test_flight_topic_updates_data_and_events(tmp_path) -> None:
    context = _context(tmp_path)

    context._on_flight_message({"source": "FAS", "data": {"fmc.tempH7": 31.2}})
    context._on_flight_message({"source": "FAS", "events": [{"name": "BOOT"}]})

    assert context.runtime.latest_flight_data == {"fmc.tempH7": 31.2}
    assert context.runtime.latest_events == [{"name": "BOOT"}]


def test_novalock_blocks_actuator_commands(tmp_path) -> None:
    import asyncio
    from app.models import CommandPayload

    context = _context(tmp_path)
    context._on_control_message({"source": "novaLock", "state": "locked"})

    with pytest.raises(ValueError, match="Nova is locked"):
        asyncio.run(context.command_service.apply_command(CommandPayload(type="solenoid", name="SVFTV", state="open")))
