import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from memtorture.cli import main
from memtorture.protocol import OPERATIONS, ProtocolError, Worker

NODE = shutil.which("node")
REFERENCE = "systems/reference-node/worker.mjs"


def test_golden_transcripts_cover_every_operation():
    path = Path("docs/protocol/v1/golden.jsonl")
    cases = [json.loads(line) for line in path.read_text().splitlines()]
    assert {case["request"]["op"] for case in cases} == OPERATIONS


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            f"{NODE} {REFERENCE}",
            marks=pytest.mark.skipif(NODE is None, reason="Node is missing"),
        ),
        f"{sys.executable} -m memtorture.worker sqlite",
        f"{sys.executable} -m memtorture.worker markdown",
        f"{sys.executable} -m memtorture.worker delayed-index",
    ],
)
def test_conformance_cli_passes_reference_and_baselines(command, capsys):
    config = "append" if command.endswith("markdown") else "synchronous=NORMAL"
    assert main(["conformance", "--command", command, "--config", config]) == 0
    assert "PASS" in capsys.readouterr().out


@pytest.mark.skipif(NODE is None, reason="Node is missing")
def test_node_worker_does_not_import_python():
    code = Path(REFERENCE).read_text()
    assert "memtorture" not in code
    assert "python" not in code.lower()
    assert all(
        "node:" in line for line in code.splitlines() if line.startswith("import ")
    )
    subprocess.run([NODE, "--check", REFERENCE], check=True)


@pytest.mark.parametrize(
    "result",
    [
        '{"id":2,"result":{"id":"r","dropped":false,"ref":null}}',
        '{"id":2,"result":{"kind":"ack","id":"wrong","dropped":false,"ref":null}}',
        "stray worker log",
        "\\xff",
    ],
)
def test_bad_stdout_invalidates_cell_without_ack(tmp_path, result):
    raw = b"\xff\n" if result == "\\xff" else (result + "\n").encode()
    script = (
        "import json,sys; "
        "sys.stdin.readline(); "
        "print(json.dumps({'id':1,'result':{'protocol':1,'capabilities':['write'],"
        "'version':'test','notes':'','servers':[]}}),flush=True); "
        "sys.stdin.readline(); "
        f"sys.stdout.buffer.write({raw!r}); "
        "sys.stdout.buffer.flush()"
    )
    from memtorture.types import Record, Scope

    with Worker(
        "bad", "default", tmp_path, command=[sys.executable, "-c", script]
    ) as worker:
        record = Record.make(Scope("t", "s"), "k", "v", id="r")
        with pytest.raises(ProtocolError):
            worker.call("write", scope=record.scope, record=record)
        assert worker.acked == {}


def test_conformance_rejects_broken_worker(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert main(["conformance", "--command", f"{sys.executable} -c 'print(123)'"]) == 1
    assert "FAIL" in capsys.readouterr().out
