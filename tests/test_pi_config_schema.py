"""Config schema for Pi I/O: tc_type, DI sensors, output limits, Heartbeat, pins."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.models import PI_RESERVED_GPIO, PiGpioSensorBinding, SystemConfig
from app.services.config_service import ConfigService

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"


def _sensor(**fields) -> dict:
    sensor = {"name": "S", "type": "TC", "binding": {"source": "TCS", "hat_id": 1, "channel_id": 0}}
    sensor.update(fields)
    return sensor


def _actuator(**fields) -> dict:
    actuator = {"name": "A", "type": "gpio_device", "binding": {"target": "TCS", "gpio_channel": 17}}
    actuator.update(fields)
    return actuator


def _load(sensors=(), actuators=(), **extra) -> SystemConfig:
    config = SystemConfig.model_validate({"Sensors": list(sensors), "Actuators": list(actuators), **extra})
    ConfigService._check_duplicates(config)
    return config


# --- existing configs ---

@pytest.mark.parametrize("name", ["system.yaml", "launch.yaml"])
def test_shipped_configs_still_load(name) -> None:
    config = ConfigService(REPO_CONFIG / name).config
    assert config.heartbeat.timeout_ms == 2000
    assert all(sensor.effective_tc_type == "K" for sensor in config.sensors if sensor.type.value == "TC")
    assert all(not actuator.actions.has_output_limits for actuator in config.actuators)


def test_saving_an_untouched_config_adds_none_of_the_new_fields(tmp_path) -> None:
    path = tmp_path / "system.yaml"
    path.write_text((REPO_CONFIG / "system.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    service = ConfigService(path)
    service.update_config(service.config.model_dump(by_alias=True))
    text = path.read_text(encoding="utf-8")
    for field in ("Heartbeat", "tc_type", "max_on_s", "on_comms_loss", "gpio_channel: null"):
        assert field not in text


# --- tc_type ---

@pytest.mark.parametrize("tc_type", ["J", "K", "T", "E", "R", "S", "B", "N"])
def test_tc_type_is_accepted_on_pi_thermocouples(tc_type) -> None:
    assert _load([_sensor(tc_type=tc_type)]).sensors[0].tc_type == tc_type


def test_tc_type_is_normalized_and_defaults_to_k() -> None:
    assert _load([_sensor(tc_type="t")]).sensors[0].tc_type == "T"
    sensor = _load([_sensor()]).sensors[0]
    assert sensor.tc_type is None and sensor.effective_tc_type == "K"


@pytest.mark.parametrize(
    "sensor",
    [
        _sensor(tc_type="X"),
        _sensor(type="PT", tc_type="K"),
        _sensor(binding={"source": "FAS", "node": "EPB_0", "channel": 1}, tc_type="K"),
    ],
)
def test_tc_type_is_rejected_elsewhere(sensor) -> None:
    with pytest.raises(ValueError):
        _load([sensor])


# --- DI sensors ---

def test_di_sensor_binds_to_a_gpio_pin() -> None:
    sensor = _load([_sensor(type="DI", binding={"source": "GCS", "gpio_channel": 5})]).sensors[0]
    assert isinstance(sensor.binding, PiGpioSensorBinding)
    assert sensor.model_dump()["binding"] == {"source": "GCS", "gpio_channel": 5}


@pytest.mark.parametrize(
    "sensor",
    [
        _sensor(type="DI"),  # hat/channel binding
        _sensor(type="DI", binding={"source": "FAS", "node": "EPB_0", "channel": 1}),
        _sensor(type="PT", binding={"source": "GCS", "gpio_channel": 5}),
        _sensor(type="DI", binding={"source": "GCS", "gpio_channel": 8}),   # reserved
        _sensor(type="DI", binding={"source": "GCS", "gpio_channel": 28}),  # outside BCM 0-27
    ],
)
def test_bad_di_sensors_are_rejected(sensor) -> None:
    with pytest.raises(ValueError):
        _load([sensor])


def test_duplicate_di_pin_is_rejected() -> None:
    di = {"type": "DI", "binding": {"source": "TCS", "gpio_channel": 5}}
    with pytest.raises(ValueError, match="Duplicate sensor address"):
        _load([_sensor(name="A", **di), _sensor(name="B", **di)])


def test_same_di_pin_on_two_pis_is_fine() -> None:
    _load([
        _sensor(name="A", type="DI", binding={"source": "TCS", "gpio_channel": 5}),
        _sensor(name="B", type="DI", binding={"source": "GCS", "gpio_channel": 5}),
    ])


def test_pin_cannot_be_input_and_output() -> None:
    with pytest.raises(ValueError, match="both an input"):
        _load([_sensor(type="DI", binding={"source": "TCS", "gpio_channel": 17})], [_actuator()])


# --- Pi GPIO actuators ---

@pytest.mark.parametrize("pin", PI_RESERVED_GPIO)
def test_reserved_pins_are_rejected_for_pi_outputs(pin) -> None:
    with pytest.raises(ValueError, match="reserved"):
        _load(actuators=[_actuator(binding={"target": "GCS", "gpio_channel": pin})])


@pytest.mark.parametrize("pin", [-1, 28, 40])
def test_pins_outside_bcm_range_are_rejected(pin) -> None:
    with pytest.raises(ValueError, match="outside BCM"):
        _load(actuators=[_actuator(binding={"target": "TCS", "gpio_channel": pin})])


@pytest.mark.parametrize("pin", [4, 5, 6, 14, 15, 17, 18, 19, 22, 23, 24, 25, 27])
def test_free_pins_are_accepted(pin) -> None:
    _load(actuators=[_actuator(binding={"target": "TCS", "gpio_channel": pin})])


@pytest.mark.parametrize("kind", ["gpio_device", "powered_gpio_device"])
def test_pi_gpio_actuator_needs_an_explicit_gpio_channel(kind) -> None:
    with pytest.raises(ValueError, match="must set binding.gpio_channel"):
        _load(actuators=[_actuator(type=kind, binding={"target": "TCS", "relay_channel": 3})])


def test_fas_gpio_actuator_keeps_the_relay_channel_fallback() -> None:
    _load(actuators=[_actuator(binding={"target": "FAS", "node": "EPB_3", "relay_channel": 2})])
    # and isn't held to the Pi's pin rules
    _load(actuators=[_actuator(binding={"target": "FAS", "node": "EPB_3", "gpio_channel": 2})])


# --- output limits ---

@pytest.mark.parametrize(
    "actuator",
    [
        _actuator(actions={"max_on_s": 600, "on_comms_loss": "safe"}),
        _actuator(type="powered_gpio_device", binding={"target": "TCS", "relay_channel": 1, "gpio_channel": 17},
                  actions={"max_on_s": 1.5}),
        _actuator(type="solenoid", binding={"target": "GCS", "relay_channel": 0}, actions={"on_comms_loss": "safe"}),
        _actuator(type="powered_device", binding={"target": "GCS", "relay_channel": 2},
                  actions={"relay_type": "nominally_off", "max_on_s": 30, "on_comms_loss": "safe"}),
        _actuator(type="motor", binding={"target": "GCS", "relay_channel": 4}, actions={"max_on_s": 10}),
    ],
)
def test_limits_are_accepted_on_pi_relay_and_gpio_outputs(actuator) -> None:
    assert _load(actuators=[actuator]).actuators[0].actions.has_output_limits


@pytest.mark.parametrize(
    "actuator",
    [
        _actuator(type="servo", binding={"target": "GCS", "relay_channel": 1, "servo_channel": 1},
                  actions={"on_comms_loss": "safe"}),
        _actuator(type="solenoid", binding={"target": "FAS", "node": "EPB_0", "relay_channel": 1},
                  actions={"max_on_s": 5}),
        _actuator(actions={"on_comms_loss": "off"}),
        _actuator(actions={"max_on_s": 0}),
        _actuator(actions={"max_on_s": -1}),
    ],
)
def test_limits_are_rejected_elsewhere(actuator) -> None:
    with pytest.raises(ValueError):
        _load(actuators=[actuator])


def test_on_comms_loss_defaults_to_hold() -> None:
    actions = _load(actuators=[_actuator()]).actuators[0].actions
    assert actions.on_comms_loss == "hold" and actions.max_on_s is None


# --- Heartbeat ---

def test_heartbeat_section() -> None:
    assert _load().heartbeat.timeout_ms == 2000
    assert _load(Heartbeat={"timeout_ms": 5000}).heartbeat.timeout_ms == 5000
    for bad in (0, 100, 600000):
        with pytest.raises(ValueError):
            _load(Heartbeat={"timeout_ms": bad})


def test_validate_config_bytes_reports_reserved_pin() -> None:
    payload = b"Actuators:\n- name: H\n  type: gpio_device\n  binding: {target: TCS, gpio_channel: 8}\n"
    with pytest.raises(ValueError, match="reserved"):
        ConfigService(Path("unused.yaml")).validate_config_bytes(payload)
