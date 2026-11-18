"""FAS bridge data-file naming: every logger prefixes its files with its own name."""
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("serial")
pytest.importorskip("paho.mqtt.client")

_spec = importlib.util.spec_from_file_location(
    "fas_bridge", Path(__file__).resolve().parents[1] / "tools" / "fas_bridge.py"
)
fas_bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fas_bridge)


@pytest.mark.parametrize(
    "given, expected",
    [
        ("2026-10-04-14_data_0", "FAS_2026-10-04-14_data_0.csv"),
        ("2026-10-04-14_data_0.csv", "FAS_2026-10-04-14_data_0.csv"),
        ("FAS_2026-10-04-14_data_0", "FAS_2026-10-04-14_data_0.csv"),
        (None, "FAS_data.csv"),
    ],
)
def test_data_file_name_is_prefixed(given, expected) -> None:
    assert fas_bridge.data_file_name(given) == expected


def test_start_data_saving_writes_prefixed_file(tmp_path) -> None:
    bridge = fas_bridge.FasBridge.__new__(fas_bridge.FasBridge)
    bridge._data_dir = str(tmp_path)
    bridge._record_file = None
    import threading

    bridge._record_lock = threading.Lock()
    bridge._log = lambda *a, **k: None
    bridge._cmd_data_file({"type": "data_file", "action": "start_data_saving", "filename": "r_data_0"})
    bridge._cmd_data_file({"type": "data_file", "action": "stop_data_saving"})
    assert [p.name for p in tmp_path.iterdir()] == ["FAS_r_data_0.csv"]
