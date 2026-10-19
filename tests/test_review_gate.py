import json
import shutil

import pytest

from memproof.cli import main
from memproof.controls import gate
from memproof.suites import history_scale
from memproof.types import NotSupported

HISTORY = {
    "discriminates": None,
    "gated": False,
    "controls": "none defined in approved spec; measurement suite",
}


def stub_controls(monkeypatch):
    def recall(system, config, path, *, paths, **kwargs):
        return {
            "paths": {
                paths[0]: {
                    "observations": [
                        {
                            "lag_ms": 100.5 if system == "delayed-index" else 0.1,
                            "polls": 1,
                        }
                    ],
                    "read_ms": {"p99": 0.1},
                }
            }
        }

    def kill(system, *args, **kwargs):
        return {"status": "violation" if system == "buffered" else "ok"}

    def concurrent(suite, system, *args, **kwargs):
        return {"status": "violation" if system == "md-rmw-sleep" else "ok"}

    monkeypatch.setattr(gate, "recall_delay", recall)
    monkeypatch.setattr(gate, "process_kill", kill)
    monkeypatch.setattr(gate, "concurrent", concurrent)


def test_quick_writes_only_inside_quick(monkeypatch, tmp_path):
    stub_controls(monkeypatch)
    top = tmp_path / "gate.json"
    top.write_text('{"full":"preserve exact bytes"}\n')
    removed = []
    original = shutil.rmtree

    def remove(path):
        removed.append(path)
        original(path)

    monkeypatch.setattr(gate.shutil, "rmtree", remove)
    gate.run_controls(quick=True, root=tmp_path)
    assert top.read_bytes() == b'{"full":"preserve exact bytes"}\n'
    assert all(tmp_path / "quick" in path.parents for path in removed)
    assert {p.name for p in tmp_path.iterdir()} == {"gate.json", "quick"}


@pytest.mark.parametrize("quick,trials", [(True, 20), (False, 5)])
def test_quick_or_short_gate_cannot_authorize_cells(tmp_path, quick, trials):
    summary = {
        "methodology_sha256": gate.methodology_hash(),
        "os": gate.platform.platform(),
        "hardware": gate.hardware(),
        "quick": quick,
        "trials": trials,
        "suites": {"process-kill": {"discriminates": True}},
    }
    (tmp_path / "gate.json").write_text(json.dumps(summary))
    with pytest.raises(NotSupported):
        gate.require_controls("process-kill", tmp_path)
    summary.update(quick=False, trials=20)
    (tmp_path / "gate.json").write_text(json.dumps(summary))
    (tmp_path / "quick").mkdir()
    (tmp_path / "quick/gate.json").write_text("{}")
    gate.require_controls("process-kill", tmp_path)


def test_history_is_ungated_without_borrowed_control_rates(monkeypatch, tmp_path):
    stub_controls(monkeypatch)
    gate.require_controls("history-scale", tmp_path / "missing")
    summary = gate.run_controls(root=tmp_path / "controls")
    assert summary["suites"]["history-scale"] == HISTORY
    result = history_scale(
        "sqlite",
        "synchronous=NORMAL",
        tmp_path / "history",
        sizes=(10,),
        repeats=1,
    )
    assert result["control_gate"] == HISTORY


def test_controls_cli_handles_ungated_measurement_suite(monkeypatch, capsys):
    monkeypatch.setattr(
        gate,
        "run_controls",
        lambda **kwargs: {
            "suites": {
                "history-scale": HISTORY,
                "process-kill": {
                    "discriminates": True,
                    "must_fail_rate": 1,
                    "must_pass_rate": 1,
                },
            }
        },
    )
    assert main(["controls", "--quick"]) == 0
    assert "history-scale: ungated" in capsys.readouterr().out
