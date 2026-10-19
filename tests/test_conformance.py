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
    "variant,valid",
    [
        *[
            (f"{kind}:{op}", False)
            for kind in ("scope", "deleted")
            for op in ("get", "list_prefix", "search", "read_recent")
        ],
        ("duplicate-upsert", False),
        ("constant-ref", False),
        ("reject-unknown-scope", False),
        ("stale-upsert-search", False),
        ("duplicate-search", False),
        ("echo-delete-ref", True),
        ("dropped-foreign-delete", True),
    ],
)
def test_worker_variants(tmp_path, node, capsys, variant, valid):
    source = variant_source(variant)
    assert source != REFERENCE.read_text()
    worker = tmp_path / "variant.mjs"
    worker.write_text(source)
    directory = tmp_path / "run"
    command = shlex.join([node, str(worker)])
    if valid:
        result = run_conformance(command, workdir=directory, visibility_timeout=0)
        assert result["passed"] > 0
        assert result["skipped"] == []
    else:
        message = {
            "duplicate-search": "duplicate",
            "stale-upsert-search": "golden case 26",
        }.get(variant)
        with pytest.raises(ProtocolError, match=message):
            run_conformance(command, workdir=directory, visibility_timeout=0)
        if variant == "duplicate-upsert":
            assert "debug diagnostic" in (directory / "worker.stderr.log").read_text()
            output = capsys.readouterr().out
            assert str(directory) in output
            assert "debug diagnostic" in output


@pytest.mark.parametrize(
    "variant", ["nearest", "wrong-first-match", "too-many-results"]
)
def test_search_variants(tmp_path, node, variant):
    source = REFERENCE.read_text().replace(KEYWORD_SEARCH, NEIGHBOUR_SEARCH)
    if variant == "wrong-first-match":
        source = source.replace(
            "Number(b.text === args.query) - Number(a.text === args.query)",
            "Number(a.text === args.query) - Number(b.text === args.query)",
        )
    elif variant == "too-many-results":
        source = source.replace(".slice(0, args.k);", ".slice(0, args.k + 1);")
    worker = tmp_path / "nearest.mjs"
    worker.write_text(source)
    command = shlex.join([node, str(worker)])
    if variant == "nearest":
        result = run_conformance(
            command, workdir=tmp_path / "run", visibility_timeout=0
        )
        assert result["skipped"] == []
    else:
        with pytest.raises(ProtocolError):
            run_conformance(command, workdir=tmp_path / "run", visibility_timeout=0)
