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


@pytest.mark.parametrize(
    "control_evidence,source_dirty,expected_dirty",
    [
        (True, False, False),
        (True, True, True),
        (False, False, True),
        (False, True, True),
    ],
)
def test_report_evidence(
    tmp_path,
    authorized_controls,
    provenance,
    control_evidence,
    source_dirty,
    expected_dirty,
):
    directory = tmp_path / "output"
    git = provenance(directory, source_dirty)
    with ResultWriter(
        directory,
        command=["memtorture", "test"],
        control_evidence=control_evidence,
    ) as writer:
        assert writer.manifest["harness_dirty"] is expected_dirty
        writer.operation(
            {"operation": "write", "suite": "x", "system": "z", "latency_ms": 1}
        )
        writer.cell({"suite": "x", "system": "z", "config": "default"})
        writer.cell({"suite": "x", "system": "a", "config": "default"})
    before = (directory / "operations.jsonl").read_bytes()
    assert main(["report", "--input", str(directory)]) == 0
    assert (directory / "operations.jsonl").read_bytes() == before
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["harness_commit"] == "a" * 40
    assert manifest["harness_dirty"] is expected_dirty
    assert manifest["hardware"]["machine"]
    assert manifest["command"] == ["memtorture", "test"]
    table = (directory / "x.md").read_text()
    assert table.index("| a |") < table.index("| z |")
    assert json.loads(before)["latency_ms"] == 1
    assert git.status_commands == [
        ["git", "status", "--porcelain"]
        + (["--", ":/", ":(top,exclude)results/controls"] if control_evidence else [])
    ]


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
