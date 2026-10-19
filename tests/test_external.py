import contextlib
import json

import pytest

from memtorture.security import check_scan
from memtorture.types import NotSupported
from scripts.fetch_qdrant import verify
from tests.helpers import make_record


@pytest.mark.parametrize(
    "verdict,complete,executed,uninspected,partial,allowed",
    [
        ("CAUTION", True, True, 0, 0, False),
        ("DO_NOT_INSTALL", True, True, 0, 0, False),
        ("SAFE", False, True, 0, 0, False),
        ("SAFE", True, False, 0, 0, False),
        ("SAFE", True, True, 1, 0, False),
        ("SAFE", True, True, 0, 1, False),
        ("SAFE", True, True, 0, 0, True),
    ],
)
def test_scan_gate(verdict, complete, executed, uninspected, partial, allowed):
    with contextlib.nullcontext() if allowed else pytest.raises(NotSupported):
        check_scan(
            {
                "risk_assessment": {"recommendation": verdict},
                "execution_successful": executed,
                "analysis_completeness": {
                    "is_complete": complete,
                    "entirely_uninspected_files": uninspected,
                    "partially_inspected_files": partial,
                },
            }
        )


def test_qdrant_checksum_mismatch_is_fatal(tmp_path):
    archive = tmp_path / "binary.tar.gz"
    archive.write_bytes(b"wrong archive")
    with pytest.raises(ValueError, match="SHA-256"):
        verify(archive, "0" * 64)


def test_native_qdrant_immediate_async_deletes_stay_deleted(adapter_factory):
    adapter = adapter_factory("qdrant", "wait=false")
    refs = []
    for index in range(20):
        record = make_record(f"key-{index}", f"memory-{index}")
        ack = adapter.write(record.scope, record)
        adapter.delete(record.scope, ack.ref)
        refs.append(ack.ref)
    barrier = make_record("barrier", "all prior updates are applied")
    adapter.bulk_load(barrier.scope, [barrier])
    assert adapter.get(barrier.scope, barrier.key) == barrier
    for ref in refs:
        assert adapter.get_by_ref(barrier.scope, ref) is None


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
