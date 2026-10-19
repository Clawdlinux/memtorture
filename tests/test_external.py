import json
from pathlib import Path

import pytest

from memtorture.adapters import load_adapter
from memtorture.security import check_scan
from memtorture.suites import process_kill
from memtorture.types import NotSupported, Record, Scope
from scripts.fetch_qdrant import verify


def test_mcp_scan_gate_rejects_incomplete_and_caution():
    for verdict, complete in [
        ("CAUTION", True),
        ("DO_NOT_INSTALL", True),
        ("SAFE", False),
    ]:
        with pytest.raises(NotSupported):
            check_scan(
                {
                    "risk_assessment": {"recommendation": verdict},
                    "execution_successful": True,
                    "analysis_completeness": {"is_complete": complete},
                }
            )


def test_qdrant_checksum_mismatch_is_fatal(tmp_path):
    archive = tmp_path / "binary.tar.gz"
    archive.write_bytes(b"wrong archive")
    with pytest.raises(ValueError, match="SHA-256"):
        verify(archive, "0" * 64)


@pytest.mark.parametrize(
    "system,config",
    [
        ("mcp-memory", "default"),
        ("qdrant", "wait=false"),
        ("qdrant", "wait=true"),
    ],
)
def test_external_conformance(system, config, tmp_path):
    adapter = load_adapter(system)
    try:
        adapter.start(tmp_path, config)
    except NotSupported as exc:
        pytest.skip(str(exc))
    try:
        assert adapter.server_pid
        scope = Scope("t", "s")
        record = Record.make(scope, "key", "needle0001")
        ack = adapter.write(scope, record)
        # Native async rows may not be visible at ack.
        import time

        deadline = time.monotonic() + 5
        while adapter.get(scope, "key") != record:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert adapter.get(Scope("other", "s"), "key") is None
        assert adapter.get_by_ref(scope, ack.ref) == record
        assert adapter.list_prefix(scope, "k", 10) == [record]
        assert adapter.read_recent(scope, 10) == [record]
        assert record in adapter.search(scope, record.text, 10, vector=record.vector)
        replacement = Record.make(scope, "key", "replacement")
        adapter.upsert(scope, replacement)
        deadline = time.monotonic() + 5
        while adapter.get(scope, "key") != replacement:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        adapter.stop()


def test_external_pins():
    manifest = json.loads(Path("scripts/mcp/package.json").read_text())
    assert manifest["dependencies"]["@modelcontextprotocol/server-memory"] == "2.0.0"


def test_native_qdrant_fault_targets(tmp_path):
    try:
        result = process_kill("qdrant", "wait=true", tmp_path, trials=1)
    except NotSupported as exc:
        pytest.skip(str(exc))
    assert result["status"] == "ok"
    assert {trial["target"] for trial in result["trials"]} == {
        "worker",
        "server:storage",
        "both",
    }


def test_prepare_never_reinstalls_known_blocked_tree(monkeypatch, tmp_path):
    from scripts import prepare_mcp

    pin = tmp_path / "scripts/mcp"
    pin.mkdir(parents=True)
    (pin / "scan-verdict.json").write_text(
        json.dumps(
            {
                "version": "2.0.0",
                "installed_dependency_tree": {"verdict": "DO_NOT_INSTALL"},
            }
        )
    )

    def forbidden_command(*args, **kwargs):
        raise AssertionError("Known blocked pins must not run npm")

    monkeypatch.setattr(prepare_mcp, "ROOT", tmp_path)
    monkeypatch.setattr(prepare_mcp.subprocess, "run", forbidden_command)
    assert prepare_mcp.main() == 1
    assert "DO_NOT_INSTALL" in (tmp_path / ".tools/mcp/blocked.txt").read_text()
