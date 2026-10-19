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
    keyword_search = (
        "return selected.reverse().filter(row => row.text.includes(args.query))"
        ".slice(0, args.k);"
    )
    if bug == "stale-upsert-search":
        assert keyword_search in code
        return (
            code.replace(
                "let rows = new Map();",
                "let rows = new Map(); const stale = new Map();",
            )
            .replace(
                "rows.delete(ref);",
                "{ stale.set(ref, row); rows.delete(ref); }",
            )
            .replace(
                keyword_search,
                "return selected.reverse().filter(row => row.text.includes(args.query))"
                ".concat([...stale.values()].filter(row => "
                "sameScope(row.scope, args.scope)))"
                ".slice(0, args.k);",
            )
        )
    if bug == "duplicate-search":
        assert keyword_search in code
        return (
            code.replace(
                "let rows = new Map();",
                "let rows = new Map(); const saved = new Map();",
            )
            .replace(
                "const value = record(args.record, args.scope);",
                "const value = record(args.record, args.scope); "
                "saved.set(value.id, value);",
            )
            .replace(
                keyword_search,
                "const hits = selected.reverse()"
                ".filter(row => row.text.includes(args.query));"
                "const other = [...saved.values()].find(row => "
                "hits.length && row.id !== hits[0].id "
                "&& sameScope(row.scope, args.scope));"
                "if (args.k >= 3 && hits.length && other) "
                "return [hits[0], hits[0], other];"
                "return hits.slice(0, args.k);",
            )
        )
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
    + [
        "duplicate-upsert",
        "constant-ref",
        "reject-unknown-scope",
        "stale-upsert-search",
        "duplicate-search",
    ],
)
def test_each_semantic_bug_fails_the_kit(tmp_path, bug):
    source = broken_source(bug)
    assert source != REFERENCE.read_text()
    worker = tmp_path / "broken.mjs"
    worker.write_text(source)
    message = {
        "duplicate-search": "duplicate",
        "stale-upsert-search": "golden case 26",
    }.get(bug)
    with pytest.raises(ProtocolError, match=message):
        conformance.run_conformance(
            shlex.join([NODE, str(worker)]),
            workdir=tmp_path / "run",
            visibility_timeout=0,
        )


@pytest.mark.skipif(NODE is None, reason="Node is missing")
@pytest.mark.parametrize("variant", ["echo-delete-ref", "dropped-foreign-delete"])
def test_valid_informational_delete_ack_variants_pass(tmp_path, variant):
    source = REFERENCE.read_text()
    original = "return ack(args.ref.id);"
    assert original in source
    replacement = (
        "return ack(args.ref.id, args.ref);"
        if variant == "echo-delete-ref"
        else "return { ...ack(args.ref.id), dropped: !visible };"
    )
    worker = tmp_path / "valid.mjs"
    worker.write_text(source.replace(original, replacement))
    result = conformance.run_conformance(
        shlex.join([NODE, str(worker)]),
        workdir=tmp_path / "run",
        visibility_timeout=0,
    )
    assert result["skipped"] == []


@pytest.mark.parametrize(
    "result",
    [
        {"kind": "ack", "id": "wrong", "dropped": True, "ref": None},
        {"id": "native", "dropped": False, "ref": None},
    ],
)
def test_delete_ack_still_requires_the_kind_and_requested_ref_id(result):
    from memtorture.protocol import validate_reply
    from memtorture.types import Ref

    with pytest.raises(ProtocolError):
        validate_reply({"id": 1, "result": result}, 1, "delete", {"ref": Ref("native")})


@pytest.mark.skipif(NODE is None, reason="Node is missing")
def test_reference_worker_ignores_vectors_and_the_legacy_id_capability(tmp_path):
    from memtorture.protocol import Worker
    from memtorture.types import Record, Scope

    with Worker(
        "reference",
        "default",
        tmp_path,
        command=[NODE, str(REFERENCE)],
    ) as worker:
        record = Record.make(Scope("t", "s"), "key", "text")
        worker.call("write", scope=record.scope, record=record)
        assert worker.call("get", scope=record.scope, key=record.key).vector is None
        assert "get-id" not in worker.capabilities


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
