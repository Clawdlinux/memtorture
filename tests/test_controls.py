import json
import platform
from argparse import Namespace

import pytest

from memtorture.cli import main
from memtorture.controls import gate
from memtorture.runner import run, run_cell
from memtorture.suites import HISTORY_CONTROLS, check_supersede_by_key, history_scale
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
    "system,machine,expected_key",
    [
        ("Darwin", "arm64", "darwin-arm64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Linux", "arm64", "linux-arm64"),
        ("Windows", "AMD64", "windows-x86_64"),
        ("Linux", "x86_64", "linux-x86_64"),
        ("FreeBSD", "RISCV64", "freebsd-riscv64"),
    ],
)
def test_platform_key(monkeypatch, system, machine, expected_key):
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    assert gate.platform_key() == expected_key


@pytest.mark.parametrize(
    "suite,change",
    [
        ("process-kill", "missing"),
        ("process-kill", "failed"),
        ("process-kill", "quick"),
        ("process-kill", "short"),
        ("process-kill", "stale"),
        ("process-kill", "os"),
        ("process-kill", "hardware"),
        ("process-kill", "other-platform"),
        ("supersede-by-key", "failed"),
    ],
)
def test_control_authorization(tmp_path, monkeypatch, change, suite):
    host_hardware = gate.hardware()
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    monkeypatch.setattr(gate, "hardware", lambda: host_hardware)
    summary = {
        "methodology_sha256": gate.methodology_hash(),
        "os": gate.platform.platform(),
        "hardware": gate.hardware(),
        "quick": False,
        "trials": 20,
        "suites": {suite: {"discriminates": True}},
    }
    changed = summary | {
        "quick": change == "quick",
        "trials": 5 if change == "short" else 20,
    }
    if change == "failed":
        changed["suites"] = {suite: {"discriminates": False}}
    elif change in {"stale", "os", "hardware"}:
        field = "methodology_sha256" if change == "stale" else change
        changed[field] = "different"
    current = tmp_path / "darwin-arm64"
    current.mkdir()
    if change == "other-platform":
        foreign = tmp_path / "other-os-arch"
        foreign.mkdir()
        (foreign / "gate.json").write_text(json.dumps(summary))
    if change not in {"missing", "other-platform"}:
        (current / "gate.json").write_text(json.dumps(changed))
    with pytest.raises(NotSupported, match="controls") as exc:
        gate.require_controls(suite, tmp_path)
    assert str(current / "gate.json") in str(exc.value)
    (current / "gate.json").write_text(json.dumps(summary))
    (current / "quick").mkdir()
    (current / "quick/gate.json").write_text("{}")
    gate.require_controls(suite, tmp_path)
    for measurement in ("history-scale", "write-throughput", "recovery"):
        gate.require_controls(measurement, tmp_path / "missing")


def test_control_evidence(stub_controls, tmp_path, monkeypatch, capsys):
    root = tmp_path / "controls"
    foreign = root / "other-os-arch"
    foreign.mkdir(parents=True)
    (foreign / "gate.json").write_text("foreign evidence\n")
    summary = gate.run_controls(root=root)
    for measurement in ("history-scale", "write-throughput", "recovery"):
        assert summary["suites"][measurement] == HISTORY_CONTROLS
    current = root / gate.platform_key()
    top = current / "gate.json"
    before = top.read_bytes()
    assert json.loads(before) == summary
    assert json.loads((current / "full/gate.json").read_text()) == summary
    assert summary["run_directory"] == str(current / "full")
    supersede = summary["suites"]["supersede-by-key"]
    assert supersede["gated"] is supersede["discriminates"] is True
    assert supersede["must_fail_rate"] == supersede["must_pass_rate"] == 1
    assert len(supersede["details"]) == 40
    gate.require_controls("supersede-by-key", root)
    quick = gate.run_controls(quick=True, root=root)
    assert top.read_bytes() == before
    assert (foreign / "gate.json").read_text() == "foreign evidence\n"
    assert {p.name for p in root.iterdir()} == {current.name, foreign.name}
    assert {p.name for p in current.iterdir()} == {"gate.json", "full", "quick"}
    assert json.loads((current / "quick/gate.json").read_text()) == quick
    assert quick["run_directory"] == str(current / "quick")
    assert quick["trials"] == 5 and quick["quick"] is True
    top.unlink()
    with pytest.raises(NotSupported, match="controls"):
        gate.require_controls("supersede-by-key", root)
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
    output = capsys.readouterr().out
    for measurement in ("history-scale", "write-throughput", "recovery"):
        assert f"{measurement}: ungated" in output
    assert "supersede-by-key: fail=100% pass=100% discriminates=True" in output


@pytest.mark.parametrize(
    "system,versions,expected",
    [
        ("append-oldest", 5, "violation"),
        ("sqlite-upsert", 5, "ok"),
        ("sqlite-upsert", 2, "ok"),
        ("last-row", 5, "violation"),
    ],
)
def test_latest_keyed_value(keyed_worker, system, versions, expected):
    events = []
    with keyed_worker(system) as worker:
        result = check_supersede_by_key(worker, versions=versions, emit=events.append)
    assert result["status"] == expected
    assert result["versions"] == versions
    assert result["acked"] == 2 * versions
    assert result["dropped"] == 0
    assert result["expected"]["key"] == "k"
    assert result["observed"]["key"] == ("decoy" if system == "last-row" else "k")
    assert result["expected"]["text"] == f"v{versions}"
    observed_text = "v1" if expected == "violation" else f"v{versions}"
    if system == "last-row":
        observed_text = f"decoy{versions}"
    assert result["observed"]["text"] == observed_text
    writes = [event for event in events if event["operation"] == "upsert"]
    assert len(writes) == 2 * versions
    assert [(event["record"]["key"], event["record"]["text"]) for event in writes] == [
        pair
        for index in range(1, versions + 1)
        for pair in (("k", f"v{index}"), ("decoy", f"decoy{index}"))
    ]
    assert len({event["record"]["id"] for event in writes}) == 2 * versions
    checks = [event for event in events if event["operation"] == "supersede-check"]
    assert len(checks) == 1
    assert checks[0]["status"] == expected


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
