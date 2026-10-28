"""Telemetry ingest: shapes, calibration, rolling averages and staleness."""
from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace

import pytest

from app import context as context_module
from app.context import AppContext
from app.models import SystemConfig
from app.services.data_service import RollingAverageStore, SensorParser, linear_interpolate

CAL = [[0, 0], [2, 100]]


def _sensor(name: str, binding: dict, method: str = "linear", calibration=CAL, unit: str = "psi") -> dict:
    convert: dict = {"method": method}
    if calibration is not None:
        convert["calibration"] = calibration
    return {"name": name, "type": "PT", "unit": unit, "binding": binding, "convert": convert}


def _config(*sensors: dict) -> SystemConfig:
    return SystemConfig.model_validate({"Sensors": list(sensors)})


GCS = {"source": "GCS", "hat_id": 0, "channel_id": 0}
FAS = {"source": "FAS", "node": "EPB_2", "channel": 1}


def _parse(config: SystemConfig, source: str, raw, calibration: bool = True, window: int = 5):
    return SensorParser(RollingAverageStore(window_size=window)).parse(source, raw, config, calibration)


# --- shapes ---

def test_gcs_list_shape() -> None:
    parsed = _parse(_config(_sensor("P", GCS)), "GCS", [{"hat_id": 0, "channel_id": 0, "value": 1.0, "timestamp": 3}])
    assert [(p.name, p.value, p.timestamp) for p in parsed] == [("P", 50.0, 3)]


def test_fas_list_and_dict_shapes_agree() -> None:
    config = _config(_sensor("P", FAS))
    item = {"node": "EPB_2", "channel": 1, "value": 1.0, "timestamp": 9}

    as_list = _parse(config, "FAS", [item])
    as_dict = _parse(config, "FAS", {"EPB_2:1": item})

    assert as_list == as_dict
    assert as_list[0].value == 50.0


@pytest.mark.parametrize(
    "item",
    [
        {"hat_id": 5, "channel_id": 5, "value": 1.0},  # not in config
        {"value": 1.0},                                  # no address at all
        {"hat_id": 9, "channel_id": 0, "value": 1.0},
    ],
)
def test_unknown_sensors_are_ignored(item) -> None:
    assert _parse(_config(_sensor("P", GCS)), "GCS", [item]) == []


def test_unknown_source_falls_back_to_gcs_bindings() -> None:
    parsed = _parse(_config(_sensor("P", GCS)), "novaGround", [{"hat_id": 0, "channel_id": 0, "value": 1.0}])
    assert len(parsed) == 1


def test_source_aliases_pick_the_right_bus() -> None:
    tcs = {"source": "TCS", "hat_id": 0, "channel_id": 0}
    config = _config(_sensor("G", GCS), _sensor("T", tcs))
    raw = [{"hat_id": 0, "channel_id": 0, "value": 1.0}]

    assert [p.name for p in _parse(config, "novaGround", raw)] == ["G"]
    assert [p.name for p in _parse(config, "novaThermo", raw)] == ["T"]


# --- calibration ---

def test_calibration_flag_off_returns_raw_volts() -> None:
    parsed = _parse(_config(_sensor("P", GCS)), "GCS", [{"hat_id": 0, "channel_id": 0, "value": 1.0}], calibration=False)
    assert parsed[0].value == 1.0
    assert parsed[0].unit == "V"


def test_calibration_flag_off_keeps_unit_when_no_conversion() -> None:
    config = _config(_sensor("P", GCS, method="none", calibration=None))
    parsed = _parse(config, "GCS", [{"hat_id": 0, "channel_id": 0, "value": 1.0}], calibration=False)
    assert parsed[0].unit == "psi"


@pytest.mark.parametrize(
    ("points", "raw", "expected"),
    [
        ([(0, 0), (2, 100)], 1.0, 50.0),
        ([(0, 0), (2, 100)], 4.0, 200.0),     # extrapolates above
        ([(0, 0), (2, 100)], -1.0, -50.0),    # and below
        ([(2, 100), (0, 0)], 1.0, 50.0),      # unsorted points
        ([(0, 5), (1, 5), (2, 5)], 7.0, 5.0),  # flat line
        ([(0, 0)], 1.0, 1.0),                 # one point: raw passes through
        (None, 1.0, 1.0),
        ([], 1.0, 1.0),
    ],
)
def test_linear_interpolate(points, raw, expected) -> None:
    assert linear_interpolate(raw, points) == pytest.approx(expected)


def test_calibration_must_be_pairs() -> None:
    with pytest.raises(ValueError):
        linear_interpolate(1.0, [(0, 0, 1), (1, 1, 1)])


@pytest.mark.parametrize("method", ["linear", "zero_offset", "polynomial"])
def test_every_non_none_method_is_calibrated(method) -> None:
    """Characterisation: today all three methods apply the same degree-1 fit.
    If zero_offset or polynomial get their own maths, update this test."""
    config = _config(_sensor("P", GCS, method=method))
    parsed = _parse(config, "GCS", [{"hat_id": 0, "channel_id": 0, "value": 1.0}])
    assert parsed[0].value == 50.0


# --- odd values ---

def test_nan_value_does_not_crash() -> None:
    parsed = _parse(_config(_sensor("P", GCS, method="none", calibration=None)), "GCS",
                    [{"hat_id": 0, "channel_id": 0, "value": float("nan")}])
    assert math.isnan(parsed[0].value)


def test_missing_value_defaults_to_zero() -> None:
    parsed = _parse(_config(_sensor("P", GCS, method="none", calibration=None)), "GCS",
                    [{"hat_id": 0, "channel_id": 0}])
    assert parsed[0].value == 0.0


def test_malformed_reading_is_skipped_and_the_rest_kept() -> None:
    config = _config(
        _sensor("A", GCS),
        _sensor("B", {"source": "GCS", "hat_id": 0, "channel_id": 1}),
    )
    raw = [
        {"hat_id": 0, "channel_id": 0, "value": None},
        {"hat_id": 0, "channel_id": 1, "value": "oops"},
        {"hat_id": 0, "channel_id": 1, "value": 1.0, "timestamp": None},
        {"hat_id": 0, "channel_id": 0, "value": 1.0, "timestamp": 4},
    ]
    parsed = _parse(config, "GCS", raw)
    assert [(p.name, p.value) for p in parsed] == [("A", 50.0)]


# --- rolling averages ---

def test_rolling_average_window() -> None:
    store = RollingAverageStore(window_size=3)
    averages = [store.add("a", v) for v in (1, 2, 3, 4)]
    assert averages == [1.0, 1.5, 2.0, 3.0]  # oldest value dropped after 3


def test_rolling_average_is_per_sensor() -> None:
    store = RollingAverageStore(window_size=3)
    store.add("a", 10)
    assert store.add("b", 2) == 2.0


# --- AppContext ingest and staleness ---

CONTEXT_CONFIG = """
Sensors:
  - name: PFT
    type: PT
    unit: psi
    binding: {source: FAS, node: EPB_1, channel: 1}
    convert: {method: none}
"""


def _ctx(tmp_path) -> tuple[AppContext, list[dict]]:
    path = tmp_path / "system.yaml"
    path.write_text(CONTEXT_CONFIG, encoding="utf-8")
    ctx = AppContext(path, tmp_path / "data")
    sent: list[dict] = []
    ctx._broadcast_from_mqtt = sent.append  # type: ignore[method-assign]
    return ctx, sent


def _engine(value: float, timestamp: int = 1) -> dict:
    return {"source": "FAS", "sensors": [{"node": "EPB_1", "channel": 1, "value": value, "timestamp": timestamp}]}


def test_identical_republish_is_not_rebroadcast(tmp_path) -> None:
    ctx, sent = _ctx(tmp_path)
    ctx._on_engine_message(_engine(5.0))
    ctx._on_engine_message(_engine(5.0))
    assert len(sent) == 1


def test_changed_value_is_broadcast_and_cached(tmp_path) -> None:
    ctx, sent = _ctx(tmp_path)
    ctx._on_engine_message(_engine(5.0, 1))
    ctx._on_engine_message(_engine(6.0, 2))

    assert len(sent) == 2
    assert ctx.runtime.latest_engine_data[0]["value"] == 6.0
    assert sent[-1]["type"] == "engine_data"


def test_out_of_order_timestamp_still_updates(tmp_path) -> None:
    """Characterisation: the newest *arrival* wins, not the newest timestamp."""
    ctx, _ = _ctx(tmp_path)
    ctx._on_engine_message(_engine(5.0, 10))
    ctx._on_engine_message(_engine(6.0, 5))
    assert ctx.runtime.latest_engine_data[0]["value"] == 6.0


def test_unknown_sensors_do_not_broadcast(tmp_path) -> None:
    ctx, sent = _ctx(tmp_path)
    ctx._on_engine_message({"source": "FAS", "sensors": [{"node": "EPB_9", "channel": 1, "value": 1.0}]})
    assert sent == []


def test_null_value_does_not_block_other_sensors(tmp_path) -> None:
    ctx, sent = _ctx(tmp_path)
    payload = _engine(None)  # type: ignore[arg-type]
    payload["sensors"].append({"node": "EPB_1", "channel": 1, "value": 3.0, "timestamp": 2})
    ctx._on_engine_message(payload)
    assert [item["value"] for item in sent[-1]["data"]] == [3.0]


def test_stale_sensor_is_dropped_and_clearing_update_broadcast(tmp_path, monkeypatch) -> None:
    ctx, sent = _ctx(tmp_path)
    now = {"t": 1000.0}
    monkeypatch.setattr(context_module, "time", SimpleNamespace(monotonic=lambda: now["t"]))
    monkeypatch.setattr(context_module, "STALE_TTL_S", 10.0)
    monkeypatch.setattr(context_module, "STALE_CHECK_INTERVAL_S", 0.001)
    broadcasts: list[dict] = []

    async def record(payload: dict) -> None:
        broadcasts.append(payload)

    ctx.broadcast = record  # type: ignore[method-assign]
    ctx._on_engine_message(_engine(5.0))
    assert ctx.runtime.latest_engine_data

    async def scenario() -> None:
        task = asyncio.create_task(ctx._staleness_loop())
        await asyncio.sleep(0.02)
        assert broadcasts == []          # still fresh
        now["t"] += 11.0
        for _ in range(200):
            await asyncio.sleep(0.01)
            if broadcasts:
                break
        task.cancel()
        await task

    asyncio.run(scenario())

    assert broadcasts == [{"type": "engine_data", "data": []}]
    assert ctx.runtime.latest_engine_data == []

    # A frozen republish of the pruned reading must not resurrect the sensor.
    sent.clear()
    ctx._on_engine_message(_engine(5.0))
    assert sent == []
