import shlex

import pytest

from memtorture.cli import main
from memtorture.conformance import run_conformance
from memtorture.protocol import ProtocolError, Worker, worker_command
from tests.helpers import (
    ADAPTER_ROWS,
    KEYWORD_SEARCH,
    NEIGHBOUR_SEARCH,
    REFERENCE,
    make_record,
    replace_source,
    variant_source,
)


@pytest.mark.parametrize("system,config", [*ADAPTER_ROWS, ("reference", "default")])
def test_adapter_contract(
    tmp_path, monkeypatch, capsys, available, system, config, request
):
    available(system)
    command = (
        [request.getfixturevalue("node"), str(REFERENCE)]
        if system == "reference"
        else worker_command(system)
    )
    monkeypatch.chdir(tmp_path)
    assert (
        main(
            [
                "conformance",
                "--command",
                shlex.join(command),
                "--config",
                config,
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    passed = next(line for line in output.splitlines() if line.startswith("PASS:"))
    assert "malformed" not in passed
    assert "HARNESS:" in output
    if system == "reference":
        with Worker(system, config, tmp_path / "storage", command=command) as worker:
            record = make_record()
            worker.call("write", scope=record.scope, record=record)
            assert worker.call("get", scope=record.scope, key=record.key).vector is None


@pytest.mark.parametrize(
    "variant,message",
    [
        ("scope:get", "get disagrees with golden reply 11"),
        ("scope:list_prefix", "list_prefix disagrees with golden reply 12"),
        ("scope:search", "search returned a record from another scope"),
        ("scope:read_recent", "read_recent disagrees with golden reply 14"),
        ("deleted:get", "get disagrees with golden reply 24"),
        ("deleted:list_prefix", "list_prefix disagrees with golden reply 25"),
        ("deleted:search", "search membership or rank disagrees with golden case 26"),
        ("deleted:read_recent", "read_recent disagrees with golden reply 27"),
        ("duplicate-upsert", "list_prefix disagrees with golden reply 19"),
        ("constant-ref", "Distinct writes returned the same native ref"),
        ("reject-unknown-scope", "write failed: invalid_request: Unknown scope field"),
        (
            "stale-upsert-search",
            "search membership or rank disagrees with golden case 26",
        ),
        ("duplicate-search", "search returned duplicate record ids"),
        ("echo-delete-ref", None),
        ("dropped-foreign-delete", None),
    ],
)
def test_worker_variants(tmp_path, node, capsys, variant, message):
    source = variant_source(variant)
    assert source != REFERENCE.read_text()
    if variant == "duplicate-upsert":
        source = replace_source(
            source,
            "function dispatch(op, args) {",
            'function dispatch(op, args) { console.error("debug diagnostic", op);',
        )
    worker = tmp_path / "variant.mjs"
    worker.write_text(source)
    directory = tmp_path / "run"
    command = shlex.join([node, str(worker)])
    if message is None:
        result = run_conformance(command, workdir=directory, visibility_timeout=0)
        assert result["passed"] > 0
        assert result["skipped"] == []
    else:
        with pytest.raises(ProtocolError, match=message):
            run_conformance(command, workdir=directory, visibility_timeout=0)
        if variant == "duplicate-upsert":
            assert "debug diagnostic" in (directory / "worker.stderr.log").read_text()
            output = capsys.readouterr().out
            assert str(directory) in output
            assert "debug diagnostic" in output


@pytest.mark.parametrize(
    "variant,message",
    [
        ("nearest", None),
        (
            "wrong-first-match",
            "search membership or rank disagrees with golden case 16",
        ),
        ("too-many-results", "search returned more than k records"),
    ],
)
def test_search_variants(tmp_path, node, variant, message):
    source = replace_source(REFERENCE.read_text(), KEYWORD_SEARCH, NEIGHBOUR_SEARCH)
    if variant == "wrong-first-match":
        source = replace_source(
            source,
            "Number(b.text === args.query) - Number(a.text === args.query)",
            "Number(a.text === args.query) - Number(b.text === args.query)",
        )
    elif variant == "too-many-results":
        source = replace_source(source, ".slice(0, args.k);", ".slice(0, args.k + 1);")
    worker = tmp_path / "nearest.mjs"
    worker.write_text(source)
    command = shlex.join([node, str(worker)])
    if message is None:
        result = run_conformance(
            command, workdir=tmp_path / "run", visibility_timeout=0
        )
        assert result["skipped"] == []
    else:
        with pytest.raises(ProtocolError, match=message):
            run_conformance(command, workdir=tmp_path / "run", visibility_timeout=0)
