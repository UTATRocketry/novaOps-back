from app.models import SystemConfig
from app.state import RuntimeState


def _solenoid(name: str) -> dict:
    return {
        "name": name,
        "type": "solenoid",
        "binding": {"target": "GCS", "relay_channel": 1},
    }


def _gpio(name: str) -> dict:
    return {
        "name": name,
        "type": "gpio_device",
        "binding": {"target": "GCS", "gpio_channel": 1},
    }


def _config(*actuators: dict) -> SystemConfig:
    return SystemConfig.model_validate({"Actuators": list(actuators)})


def test_switching_configs_adds_removes_and_keeps_actuators() -> None:
    runtime = RuntimeState()
    runtime.initialize_all(_config(_solenoid("keep"), _solenoid("gone")))
    runtime.actuator_states["keep"]["position"] = "open"

    runtime.sync_actuators(_config(_solenoid("keep"), _solenoid("new")))

    assert set(runtime.actuator_states) == {"keep", "new"}
    assert runtime.actuator_states["keep"] == {"position": "open"}
    assert runtime.actuator_states["new"] == {"position": "closed"}


def test_type_change_resets_state() -> None:
    runtime = RuntimeState()
    runtime.initialize_all(_config(_solenoid("valve")))
    runtime.actuator_states["valve"]["position"] = "open"

    runtime.sync_actuators(_config(_gpio("valve")))

    assert runtime.actuator_states["valve"] == {"arming": "disarmed"}


def test_sync_from_empty_matches_initialize_all() -> None:
    config = _config(_solenoid("a"), _gpio("b"))
    synced, initialized = RuntimeState(), RuntimeState()

    synced.sync_actuators(config)
    initialized.initialize_all(config)

    assert synced.actuator_states == initialized.actuator_states
