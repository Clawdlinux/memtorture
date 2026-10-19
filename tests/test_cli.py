import json
import subprocess
import sys
from argparse import Namespace

import pytest

from memtorture.cli import main
from memtorture.results import ResultWriter
from memtorture.runner import ROWS, run


@pytest.mark.parametrize(
    "args,expected",
    [
        ([], "evidence, not proof"),
        (["controls"], "--quick"),
        (["run"], "not disk durability"),
    ],
)
def test_help(args, expected):
    result = subprocess.run(
        [sys.executable, "-m", "memtorture", *args, "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout


def test_invalid_row():
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "run",
                "--suite",
                "process-kill",
                "--system",
                "qdrant",
                "--config",
                "invalid-row",
            ]
        )
    assert exc.value.code != 0


def test_report_evidence(tmp_path, authorized_controls):
    with ResultWriter(tmp_path, command=["memtorture", "test"]) as writer:
        writer.operation(
            {"operation": "write", "suite": "x", "system": "z", "latency_ms": 1}
        )
        writer.cell({"suite": "x", "system": "z", "config": "default"})
        writer.cell({"suite": "x", "system": "a", "config": "default"})
    before = (tmp_path / "operations.jsonl").read_bytes()
    assert main(["report", "--input", str(tmp_path)]) == 0
    assert (tmp_path / "operations.jsonl").read_bytes() == before
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["harness_commit"]
    assert manifest["hardware"]["machine"]
    assert manifest["command"] == ["memtorture", "test"]
    table = (tmp_path / "x.md").read_text()
    assert table.index("| a |") < table.index("| z |")
    assert json.loads(before)["latency_ms"] == 1


def test_run_matrix(authorized_controls, monkeypatch, tmp_path):
    calls = []

    def cell(suite, system, config, path, **kwargs):
        calls.append((suite, system, config))
        return {"status": "ok"}

    monkeypatch.setattr("memtorture.runner.run_cell", cell)
    args = Namespace(
        all=True,
        output=tmp_path / "run",
        extended=False,
        invocation=["memtorture", "run", "--all"],
    )
    assert run(args) == 0
    assert sum(map(len, ROWS.values())) == 7
    assert len(calls) == len(set(calls)) == 35
    assert len(json.loads((args.output / "cells.json").read_text())) == 35


def test_conformance_failure(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert main(["conformance", "--command", f"{sys.executable} -c 'print(123)'"]) == 1
    assert "FAIL" in capsys.readouterr().out
