import json
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from memtorture.cli import main
from memtorture.controls import gate
from memtorture.results import LOCAL_BANNER, ResultWriter
from memtorture.runner import ROWS, run
from memtorture.suites import SUITES


@pytest.mark.parametrize(
    "args",
    [[], ["controls"], ["run"]],
)
def test_help(args):
    result = subprocess.run(
        [sys.executable, "-m", "memtorture", *args, "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    if not args:
        assert all(
            command in result.stdout
            for command in (
                "controls",
                "conformance",
                "run",
                "report",
                "export-system",
            )
        )


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
def test_manifest_provenance(
    workspace, provenance, control_evidence, source_dirty, expected_dirty
):
    directory = (
        Path("results/controls") / gate.platform_key() / "full"
        if control_evidence
        else Path("results/runs/provenance")
    )
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
    assert {path.name for path in directory.iterdir()} == {
        "cells.json",
        "manifest.json",
        "operations.jsonl",
    }
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["harness_commit"] == "a" * 40
    assert manifest["harness_dirty"] is expected_dirty
    assert manifest["hardware"]["machine"]
    assert manifest["platform"] == gate.platform_key()
    assert manifest["command"] == ["memtorture", "test"]
    assert [row["system"] for row in manifest["systems"]] == ["a", "z"]
    assert json.loads((directory / "operations.jsonl").read_text())["latency_ms"] == 1
    assert git.status_commands == [
        ["git", "status", "--porcelain"]
        + (["--", ":/", ":(top,exclude)results/controls"] if control_evidence else [])
    ]


def test_local_report_and_export(workspace, authorized_controls, provenance, capsys):
    run_dir = Path("results/runs/20261020T000000Z-local")
    provenance(run_dir, False)
    with ResultWriter(run_dir, command=["memtorture", "test"]) as writer:
        for system, latency in (("sqlite", 111111), ("markdown", 987654)):
            writer.operation(
                {"operation": "write", "suite": "x", "system": system, "ms": latency}
            )
            writer.cell(
                {"suite": "x", "system": system, "config": "default", "ms": latency}
            )
        writer.operation(
            {"operation": "cell-blocked", "suite": "y", "system": "markdown"}
        )
    files = {path.name: path.read_bytes() for path in run_dir.iterdir()}
    assert set(files) == {"cells.json", "manifest.json", "operations.jsonl"}

    assert main(["report", "--input", str(run_dir)]) == 1
    assert "report --local" in capsys.readouterr().out
    assert main(["report", "--local"]) == 0
    output = capsys.readouterr().out
    assert output.startswith(LOCAL_BANNER + "\n")
    assert output.rstrip().endswith(LOCAL_BANNER)
    assert output.index("| markdown |") < output.index("| sqlite |")
    assert "987654" in output and "111111" in output
    assert {path.name: path.read_bytes() for path in run_dir.iterdir()} == files
    assert not list(workspace.rglob("*.md"))
    assert main(["report", "--local", "--input", str(workspace)]) == 1
    assert "results/runs" in capsys.readouterr().out

    assert main(["export-system", "--system", "sqlite"]) == 0
    export = Path("results/runs/exports") / f"{run_dir.name}-sqlite"
    assert str(export) in capsys.readouterr().out
    manifest = json.loads((export / "manifest.json").read_text())
    assert manifest["harness_commit"] == "a" * 40
    assert manifest["harness_dirty"] is True
    assert manifest["platform"] == gate.platform_key()
    assert manifest["exported_from"] == run_dir.name
    assert manifest["systems"] == [{"system": "sqlite", "config": "default"}]
    cells = json.loads((export / "cells.json").read_text())
    assert cells == [
        {"suite": "x", "system": "sqlite", "config": "default", "ms": 111111}
    ]
    operations = (export / "operations.jsonl").read_text().splitlines()
    assert [json.loads(line)["system"] for line in operations] == ["sqlite"]
    exported = "".join(path.read_text() for path in export.iterdir())
    assert "markdown" not in exported and "987654" not in exported

    assert main(["export-system", "--system", "sqlite"]) == 1
    assert main(["export-system", "--system", "qdrant"]) == 1
    assert "qdrant" in capsys.readouterr().out
    outside = workspace / "issue"
    assert main(["export-system", "--system", "sqlite", "--out", str(outside)]) == 1
    assert "results/runs" in capsys.readouterr().out
    assert not outside.exists()


@pytest.mark.parametrize(
    "case,path,control_evidence,allowed",
    [
        ("run", "results/runs/a", False, True),
        ("nested run", "results/runs/exports/a", False, True),
        ("runs root", "results/runs", False, False),
        ("dotdot escape", "results/runs/../runs-evil/a", False, False),
        ("outside", "out", False, False),
        ("run in controls", "results/controls/{platform}/a", False, False),
        ("symlink escape", "results/runs/link/a", False, False),
        ("controls", "results/controls/{platform}/full", True, True),
        ("controls root", "results/controls/{platform}", True, False),
        ("other platform", "results/controls/other-os-arch/full", True, False),
        ("controls in runs", "results/runs/a", True, False),
    ],
)
def test_output_policy(workspace, provenance, case, path, control_evidence, allowed):
    elsewhere = workspace / "elsewhere"
    elsewhere.mkdir()
    (workspace / "results/runs").mkdir(parents=True)
    (workspace / "results/runs/link").symlink_to(elsewhere)
    path = Path(path.format(platform=gate.platform_key()))
    provenance(path / "never", False)
    writer = lambda: ResultWriter(  # noqa: E731
        path, command=["memtorture", "test"], control_evidence=control_evidence
    )
    if allowed:
        with writer():
            pass
        assert (path / "manifest.json").is_file(), case
        return
    with pytest.raises(ValueError, match="Output must be under") as exc:
        writer()
    expected = "results/controls/" if control_evidence else "results/runs/"
    assert expected in str(exc.value), case
    assert not (path / "manifest.json").exists(), case
    assert not list(elsewhere.iterdir()), case
    if not control_evidence:
        with pytest.raises(SystemExit) as stopped:
            main(
                [
                    "run",
                    "--suite",
                    "process-kill",
                    "--system",
                    "sqlite",
                    "--config",
                    "synchronous=FULL",
                    "--output",
                    str(path),
                ]
            )
        assert stopped.value.code == 1, case
        assert not list(elsewhere.iterdir()), case


def test_run_matrix(authorized_controls, monkeypatch, tmp_path, workspace):
    calls = []
    evidence = {"current-platform": True}
    current = tmp_path / "controls" / gate.platform_key()
    current.mkdir(parents=True)
    (current / "gate.json").write_text(json.dumps(evidence))

    def cell(suite, system, config, path, **kwargs):
        calls.append((suite, system, config))
        return {"status": "ok"}

    monkeypatch.setattr("memtorture.runner.run_cell", cell)
    args = Namespace(
        all=True,
        output=Path("results/runs/matrix"),
        extended=False,
        invocation=["memtorture", "run", "--all"],
    )
    assert run(args) == 0
    assert not list(args.output.glob("*.md"))
    expected = {
        (suite, system, config)
        for suite in SUITES
        for system, configs in ROWS.items()
        for config in configs
    }
    assert len(calls) == len(set(calls)) == len(expected)
    assert set(calls) == expected
    assert len(json.loads((args.output / "cells.json").read_text())) == len(expected)
    manifest = json.loads((args.output / "manifest.json").read_text())
    assert manifest["controls"] == evidence


def test_conformance_failure(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert main(["conformance", "--command", f"{sys.executable} -c 'print(123)'"]) == 1
    assert "FAIL" in capsys.readouterr().out
