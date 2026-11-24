"""Thermocouple fault values, DI readings and telemetry sources."""
from __future__ import annotations

import logging

import pytest

from app.models import SystemConfig
from app.services.data_service import RollingAverageStore, SensorParser

CONFIG = SystemConfig.model_validate({
    "Sensors": [
        {"name": "TC_T", "type": "TC", "unit": "C", "binding": {"source": "TCS", "hat_id": 1, "channel_id": 0},
         "convert": {"method": "none"}},
        {"name": "TC_G", "type": "TC", "unit": "C", "binding": {"source": "GCS", "hat_id": 1, "channel_id": 0},
         "convert": {"method": "none"}},
        {"name": "PT_G", "type": "PT", "unit": "psi", "binding": {"source": "GCS", "hat_id": 0, "channel_id": 0},
         "convert": {"method": "none"}},
        {"name": "TC_F", "type": "TC", "unit": "C", "binding": {"source": "FAS", "node": "EPB_0", "channel": 1},
         "convert": {"method": "none"}},
        {"name": "DOOR", "type": "DI", "binding": {"source": "TCS", "gpio_channel": 5}},
        {"name": "LID", "type": "DI", "unit": "", "binding": {"source": "TCS", "gpio_channel": 6}},
        {"name": "G_DI", "type": "DI", "binding": {"source": "GCS", "gpio_channel": 5}},
    ]
})


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _parser(clock=None) -> SensorParser:
    return SensorParser(RollingAverageStore(window_size=5), clock=clock or _Clock())


def _tc(value, timestamp=1) -> list[dict]:
    return [{"hat_id": 1, "channel_id": 0, "value": value, "timestamp": timestamp}]


# --- thermocouple faults ---

@pytest.mark.parametrize(
    ("raw", "fault"),
    [(-9999.0, "open"), (-7777.0, "common_mode"), (-8888.0, "overrange"), (-9999, "open")],
)
def test_tc_fault_values_become_faults(raw, fault) -> None:
    [reading] = _parser().parse("novaThermo", _tc(raw), CONFIG, True)
    assert reading.fault == fault
    assert reading.value is None and reading.avg is None
    assert reading.to_dict() == {"name": "TC_T", "value": None, "avg": None, "unit": "C",
                                 "timestamp": 1, "fault": fault}


def test_fault_is_left_out_of_the_rolling_average() -> None:
    parser = _parser()
    parser.parse("novaThermo", _tc(20.0, 1), CONFIG, True)
    parser.parse("novaThermo", _tc(-9999.0, 2), CONFIG, True)
    [reading] = parser.parse("novaThermo", _tc(30.0, 3), CONFIG, True)
    assert reading.avg == 25.0 and reading.fault is None


def test_good_reading_has_no_fault_key() -> None:
    [reading] = _parser().parse("GCS", _tc(21.5), CONFIG, True)
    assert "fault" not in reading.to_dict()


@pytest.mark.parametrize("value", [-9999.5, -9998.0, -7777.1])
def test_only_exact_sentinels_are_faults(value) -> None:
    [reading] = _parser().parse("novaThermo", _tc(value), CONFIG, True)
    assert reading.fault is None and reading.value == round(value, 2)


def test_sentinel_on_a_non_tc_sensor_is_a_value() -> None:
    raw = [{"hat_id": 0, "channel_id": 0, "value": -9999.0, "timestamp": 1}]
    [reading] = _parser().parse("GCS", raw, CONFIG, True)
    assert reading.fault is None and reading.value == -9999.0


def test_fas_thermocouples_are_left_alone() -> None:
    raw = [{"node": "EPB_0", "channel": 1, "value": -9999.0, "timestamp": 1}]
    [reading] = _parser().parse("FAS", raw, CONFIG, True)
    assert reading.fault is None and reading.value == -9999.0


# --- DI readings from gpios ---

def test_gpios_become_di_readings() -> None:
    gpios = [{"pin_id": 5, "state": 1}, {"pin_id": 6, "state": 0}, {"pin_id": 17, "state": 1},
             {"pin_id": "x", "state": 1}, "junk", {"pin_id": 6}]
    parsed = _parser().parse("novaThermo", _tc(20.0, 1234), CONFIG, True, gpios=gpios)
    di = {p.name: p for p in parsed if p.name in ("DOOR", "LID")}
    assert {name: (p.value, p.avg) for name, p in di.items()} == {"DOOR": (1, 1), "LID": (0, 0)}
    # No timestamp of their own: they take the packet's newest sensor timestamp.
    assert di["DOOR"].timestamp == 1234


def test_gpios_are_matched_per_pi() -> None:
    parsed = _parser().parse("novaGround", [], CONFIG, True, gpios=[{"pin_id": 5, "state": 0}])
    assert [(p.name, p.value) for p in parsed] == [("G_DI", 0)]


def test_true_and_nonzero_states_read_as_one() -> None:
    parsed = _parser().parse("TCS", [], CONFIG, True, gpios=[{"pin_id": 5, "state": True},
                                                             {"pin_id": 6, "state": 3}])
    assert [p.value for p in parsed] == [1, 1]
    assert all(p.timestamp > 0 for p in parsed)


# --- sources ---

def test_novamock_counts_as_gcs() -> None:
    parsed = _parser().parse("novaMock", _tc(20.0), CONFIG, True)
    assert [p.name for p in parsed] == ["TC_G"]


def test_unknown_source_is_dropped_and_logged_once(caplog) -> None:
    parser = _parser()
    with caplog.at_level(logging.WARNING):
        assert parser.parse("novaBogus", _tc(20.0), CONFIG, True) == []
        assert parser.parse("novaBogus", _tc(21.0), CONFIG, True) == []
        assert parser.parse("", _tc(21.0), CONFIG, True) == []
    unknown = [r for r in caplog.records if "unknown source" in r.getMessage()]
    assert len(unknown) == 2  # once for novaBogus, once for ""


def test_two_sources_for_one_target_warns(caplog) -> None:
    clock = _Clock()
    parser = _parser(clock)
    with caplog.at_level(logging.WARNING):
        parser.parse("novaGround", _tc(20.0), CONFIG, True)
        clock.now = 1.0
        parser.parse("novaMock", _tc(20.0, 2), CONFIG, True)
        clock.now = 2.0
        parser.parse("novaGround", _tc(20.0, 3), CONFIG, True)  # same pair: not again
    warnings = [r for r in caplog.records if "more than one source" in r.getMessage()]
    assert len(warnings) == 1
    assert "novaground" in warnings[0].getMessage() and "novamock" in warnings[0].getMessage()


def test_sources_far_apart_in_time_do_not_warn(caplog) -> None:
    clock = _Clock()
    parser = _parser(clock)
    with caplog.at_level(logging.WARNING):
        parser.parse("novaGround", _tc(20.0), CONFIG, True)
        clock.now = 30.0
        parser.parse("novaMock", _tc(20.0, 2), CONFIG, True)
        parser.parse("novaThermo", _tc(20.0, 2), CONFIG, True)  # another target
    assert not [r for r in caplog.records if "more than one source" in r.getMessage()]


# --- through AppContext ---

def test_engine_message_with_only_gpio_changes_is_not_deduplicated(tmp_path) -> None:
    from app.context import AppContext

    path = tmp_path / "system.yaml"
    path.write_text(
        "Sensors:\n- name: DOOR\n  type: DI\n  binding: {source: TCS, gpio_channel: 5}\n", encoding="utf-8")
    ctx = AppContext(path, tmp_path / "data")
    sensors = [{"hat_id": 0, "channel_id": 0, "value": 1.0, "timestamp": 10}]
    ctx._on_engine_message({"source": "novaThermo", "sensors": sensors, "gpios": [{"pin_id": 5, "state": 0}]})
    # Same analog readings, only the input changed.
    ctx._on_engine_message({"source": "novaThermo", "sensors": sensors, "gpios": [{"pin_id": 5, "state": 1}]})
    assert ctx.runtime.latest_engine_data == [
        {"name": "DOOR", "value": 1, "avg": 1, "unit": "", "timestamp": 10}]
