import json
import shlex
import shutil
from pathlib import Path

import pytest

from memtorture import conformance
from memtorture.cli import main
from memtorture.protocol import ProtocolError

NODE = shutil.which("node")
REFERENCE = Path(__file__).resolve().parents[1] / "systems/reference-node/worker.mjs"
SELECTED = (
    "const selected = [...rows.values()]"
    ".filter(row => sameScope(row.scope, args.scope));"
)


def broken_source(bug):
    code = REFERENCE.read_text()
    if bug.startswith("scope:"):
        op = bug.split(":")[1]
        return code.replace(
            SELECTED,
            "const selected = [...rows.values()].filter(row => "
            f'op === "{op}" || sameScope(row.scope, args.scope));',
        )
    if bug.startswith("deleted:"):
        op = bug.split(":")[1]
        code = code.replace(
            "let rows = new Map();",
            "let rows = new Map(); const deletedRows = new Map();",
        ).replace(
            "if (visible) rows.delete(args.ref.id);",
            "if (visible) { deletedRows.set(args.ref.id, found); "
            "rows.delete(args.ref.id); }",
        )
        return code.replace(
            SELECTED,
            "const selected = [...rows.values(), "
            f'...(op === "{op}" ? deletedRows.values() : [])]'
            ".filter(row => sameScope(row.scope, args.scope));",
        )
    if bug == "duplicate-upsert":
        return code.replace("if (replace) {", "if (false) {")
    if bug == "constant-ref":
        return (
            code.replace(
                'return ack(value.id, { kind: "ref", id: ref });',
                'return ack(value.id, { kind: "ref", id: "constant" });',
            )
            .replace(
                "const found = rows.get(args.ref.id);",
                "const found = [...rows.values()].reverse()"
                '.find(row => row.key === "path/one");',
            )
            .replace(
                "if (visible) rows.delete(args.ref.id);",
                "if (visible) { for (const [id, row] of rows) "
                "{ if (row.id === found.id) rows.delete(id); } }",
            )
        )
    assert bug == "reject-unknown-scope"
    return code.replace(
        "scope(args.scope);",
        'scope(args.scope); check(!("future" in args.scope), "Unknown scope field");',
    )


@pytest.mark.skipif(NODE is None, reason="Node is missing")
@pytest.mark.parametrize(
    "bug",
    [
        f"{kind}:{op}"
        for kind in ("scope", "deleted")
        for op in ("get", "list_prefix", "search", "read_recent")
    ]
    + ["duplicate-upsert", "constant-ref", "reject-unknown-scope"],
)
def test_each_semantic_bug_fails_the_kit(tmp_path, bug):
    source = broken_source(bug)
    assert source != REFERENCE.read_text()
    worker = tmp_path / "broken.mjs"
    worker.write_text(source)
    with pytest.raises(ProtocolError):
        conformance.run_conformance(
            shlex.join([NODE, str(worker)]),
            workdir=tmp_path / "run",
            visibility_timeout=0,
        )


@pytest.mark.skipif(NODE is None, reason="Node is missing")
def test_failure_keeps_stderr_and_reports_tail(tmp_path, capsys):
    code = broken_source("duplicate-upsert").replace(
        "function dispatch(op, args) {",
        'function dispatch(op, args) { console.error("debug diagnostic", op);',
    )
    worker = tmp_path / "broken.mjs"
    worker.write_text(code)
    directory = tmp_path / "retained"
    with pytest.raises(ProtocolError):
        conformance.run_conformance(
            shlex.join([NODE, str(worker)]),
            workdir=directory,
            visibility_timeout=0,
        )
    assert (directory / "worker.stderr.log").is_file()
    assert "debug diagnostic" in (directory / "worker.stderr.log").read_text()
    output = capsys.readouterr().out
    assert str(directory) in output
    assert "debug diagnostic" in output


@pytest.mark.skipif(NODE is None, reason="Node is missing")
def test_cli_separates_worker_checks_from_harness_self_checks(capsys):
    assert main(["conformance", "--command", shlex.join([NODE, str(REFERENCE)])]) == 0
    output = capsys.readouterr().out.splitlines()
    passed = next(line for line in output if line.startswith("PASS:"))
    assert "malformed" not in passed
    assert "HARNESS:" in "\n".join(output)


def test_malformed_cases_use_the_real_reply_parser(monkeypatch):
    from memtorture.protocol import parse_reply

    seen = []

    def observe(line, *args):
        seen.append(line)
        return parse_reply(line, *args)

    monkeypatch.setattr(conformance, "parse_reply", observe)
    conformance.check_malformed()
    assert b"\xff\n" in seen
    assert b"worker log\n" in seen


def test_raw_conformance_args_preserve_scope_extensions():
    from memtorture.protocol import Worker

    assert callable(Worker.call_raw)
    cases = conformance.cases("golden.jsonl")
    writes = [c for c in cases if c["request"]["op"] == "write"]
    assert len(writes) >= 2
    assert "future" in writes[0]["request"]["args"]["scope"]
    assert json.loads(REFERENCE.with_name("manifest.json").read_text())["command"]
