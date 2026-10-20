import json
from argparse import Namespace

import pytest

from memtorture.cli import main
from memtorture.controls import gate
from memtorture.runner import run, run_cell
from memtorture.suites import HISTORY_CONTROLS, history_scale
from memtorture.types import NotSupported


@pytest.mark.parametrize(
    "failures,passes,trials,allowed",
    [
        (8, 10, 10, False),
        (9, 10, 10, True),
        (10, 9, 10, False),
        (0, 0, 0, False),
    ],
)
def test_gate_threshold(failures, passes, trials, allowed):
    assert gate.control_gate(failures, passes, trials) is allowed


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "failed",
        "quick",
        "short",
        "stale",
        "os",
        "hardware",
    ],
)
def test_control_authorization(tmp_path, change):
    summary = {
        "methodology_sha256": gate.methodology_hash(),
        "os": gate.platform.platform(),
        "hardware": gate.hardware(),
        "quick": False,
        "trials": 20,
        "suites": {"process-kill": {"discriminates": True}},
    }
    changed = summary | {
        "quick": change == "quick",
        "trials": 5 if change == "short" else 20,
    }
    if change == "failed":
        changed["suites"] = {"process-kill": {"discriminates": False}}
    elif change in {"stale", "os", "hardware"}:
        field = "methodology_sha256" if change == "stale" else change
        changed[field] = "different"
    if change != "missing":
        (tmp_path / "gate.json").write_text(json.dumps(changed))
    with pytest.raises(NotSupported, match="controls"):
        gate.require_controls("process-kill", tmp_path)
    (tmp_path / "gate.json").write_text(json.dumps(summary))
    (tmp_path / "quick").mkdir()
    (tmp_path / "quick/gate.json").write_text("{}")
    gate.require_controls("process-kill", tmp_path)
    gate.require_controls("history-scale", tmp_path / "missing")


def test_control_evidence(stub_controls, tmp_path, monkeypatch, capsys):
    summary = gate.run_controls(root=tmp_path / "controls")
    assert summary["suites"]["history-scale"] == HISTORY_CONTROLS
    top = tmp_path / "controls/gate.json"
    before = top.read_bytes()
    quick = gate.run_controls(quick=True, root=tmp_path / "controls")
    assert top.read_bytes() == before
    assert {p.name for p in (tmp_path / "controls").iterdir()} == {
        "gate.json",
        "full",
        "quick",
    }
    assert (tmp_path / "controls/quick/gate.json").is_file()
    result = history_scale(
        "sqlite",
        "synchronous=NORMAL",
        tmp_path / "history",
        sizes=(10,),
        repeats=1,
    )
    assert result["control_gate"] == HISTORY_CONTROLS
    monkeypatch.setattr(gate, "run_controls", lambda **kwargs: quick)
    assert main(["controls", "--quick"]) == 0
    assert "history-scale: ungated" in capsys.readouterr().out


def test_blocked_cells(blocked_controls, tmp_path):
    with pytest.raises(NotSupported, match="controls"):
        run_cell("process-kill", "sqlite", "synchronous=FULL", tmp_path / "cell")
    args = Namespace(
        all=True,
        output=tmp_path / "run",
        extended=False,
        invocation=["memtorture", "run", "--all"],
    )
    assert run(args) == 1
    assert json.loads((args.output / "cells.json").read_text()) == []
    assert not list(args.output.glob("*.md"))


@pytest.mark.parametrize(
    "relative",
    [
        "docs/PROTOCOL.md",
        "docs/protocol/v1/embed-vectors.json",
        "systems/sqlite/manifest.json",
        "systems/reference-node/worker.mjs",
    ],
)
def test_contract_hash(tmp_path, monkeypatch, relative):
    module = tmp_path / "memtorture/controls/gate.py"
    module.parent.mkdir(parents=True)
    module.write_text("")
    monkeypatch.setattr(gate, "__file__", str(module))
    before = gate.methodology_hash()
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed contract\n")
    assert gate.methodology_hash() != before
