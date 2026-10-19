import json
from argparse import Namespace

import pytest

from memproof.cli import main
from memproof.results import ResultWriter
from memproof.runner import ROWS, run, run_cell
from memproof.types import NotSupported


def test_run_help_mentions_fault_scope(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["run", "--help"])
    assert exc.value.code == 0
    assert "not disk durability" in capsys.readouterr().out


def test_unknown_row_is_rejected():
    assert "durable" not in ROWS["qdrant"]
    with pytest.raises(SystemExit):
        main(
            [
                "run",
                "--suite",
                "process-kill",
                "--system",
                "qdrant",
                "--config",
                "durable",
            ]
        )


def test_cell_requires_current_controls(monkeypatch, tmp_path):
    def blocked(suite):
        raise NotSupported("controls do not discriminate")

    monkeypatch.setattr("memproof.runner.require_controls", blocked)
    with pytest.raises(NotSupported, match="controls"):
        run_cell("process-kill", "sqlite", "synchronous=FULL", tmp_path)


def test_report_preserves_raw_operations(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("memproof.runner.require_controls", lambda suite: None)
    with ResultWriter(tmp_path, ["test"]) as writer:
        writer.operation({"operation": "write", "suite": "recall-delay"})
        writer.cell(
            {
                "system": "sqlite",
                "config": "synchronous=FULL",
                "suite": "recall-delay",
                "status": "ok",
            }
        )
    before = (tmp_path / "operations.jsonl").read_bytes()
    assert main(["report", "--input", str(tmp_path)]) == 0
    assert before == (tmp_path / "operations.jsonl").read_bytes()
    assert json.loads((tmp_path / "manifest.json").read_text())["command"] == ["test"]


def test_all_cell_rows_are_declared():
    assert sum(map(len, ROWS.values())) == 7


def test_all_runs_35_cells(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("memproof.runner.require_controls", lambda suite: None)
    monkeypatch.setattr("memproof.runner.DEFAULT_ROOT", tmp_path / "controls")

    def cell(suite, system, config, path, **kwargs):
        calls.append((suite, system, config))
        return {"status": "ok"}

    monkeypatch.setattr("memproof.runner.run_cell", cell)
    output = tmp_path / "run"
    args = Namespace(
        all=True, output=output, extended=False, invocation=["memproof", "run", "--all"]
    )
    assert run(args) == 0
    assert len(calls) == 35
    assert len(set(calls)) == 35
    assert len(json.loads((output / "cells.json").read_text())) == 35


def test_bad_controls_publish_no_cells(monkeypatch, tmp_path):
    def blocked(suite):
        raise NotSupported("controls missing")

    monkeypatch.setattr("memproof.runner.require_controls", blocked)
    monkeypatch.setattr("memproof.runner.DEFAULT_ROOT", tmp_path / "controls")
    args = Namespace(
        all=True,
        output=tmp_path / "run",
        extended=False,
        invocation=["memproof", "run", "--all"],
    )
    assert run(args) == 1
    assert json.loads((args.output / "cells.json").read_text()) == []
    assert not list(args.output.glob("*.md"))
