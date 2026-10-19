import shlex
import shutil
import sys
from pathlib import Path

import pytest

from memtorture.conformance import run_conformance
from memtorture.protocol import ProtocolError
from memtorture.runner import ROWS
from memtorture.types import Scope

NODE = shutil.which("node")
REFERENCE = Path(__file__).resolve().parents[1] / "systems/reference-node/worker.mjs"
KEYWORD_SEARCH = (
    "return selected.reverse().filter(row => row.text.includes(args.query))"
    ".slice(0, args.k);"
)
NEIGHBOUR_SEARCH = (
    "return selected.sort((a, b) => "
    "Number(b.text === args.query) - Number(a.text === args.query)).slice(0, args.k);"
)


def test_qdrant_zero_k_is_empty_without_querying_a_server():
    from memtorture.adapters.qdrant import Qdrant

    assert Qdrant().search(Scope("t", "s"), "text", 0, vector=[0.0] * 64) == []


@pytest.mark.parametrize(
    "system,config",
    [(system, config) for system, rows in sorted(ROWS.items()) for config in rows],
)
def test_every_available_python_adapter_passes_conformance(tmp_path, system, config):
    if system == "mcp-memory":
        from memtorture.adapters.mcp_memory import BLOCK_REASON

        pytest.skip(BLOCK_REASON)
    if system == "qdrant":
        from memtorture.adapters.qdrant import ROOT, VERSION

        artifact = ROOT / ".tools/qdrant" / VERSION / "qdrant"
        if not artifact.is_file():
            pytest.skip(f"Pinned Qdrant artifact is missing: {artifact}")
    result = run_conformance(
        shlex.join([sys.executable, "-m", "memtorture.worker", system]),
        config=config,
        workdir=tmp_path / "cell",
    )
    assert result["passed"] > 0
    assert all("retrieve_for" in reason for reason in result["skipped"])


@pytest.mark.skipif(NODE is None, reason="Node is missing")
def test_nearest_neighbour_search_may_return_unrelated_records(tmp_path):
    source = REFERENCE.read_text()
    assert KEYWORD_SEARCH in source
    worker = tmp_path / "nearest.mjs"
    worker.write_text(source.replace(KEYWORD_SEARCH, NEIGHBOUR_SEARCH))
    result = run_conformance(
        shlex.join([NODE, str(worker)]),
        workdir=tmp_path / "run",
        visibility_timeout=0,
    )
    assert result["skipped"] == []


@pytest.mark.skipif(NODE is None, reason="Node is missing")
@pytest.mark.parametrize("bug", ["wrong-first-match", "too-many-results"])
def test_neighbour_search_still_requires_rank_and_limit(tmp_path, bug):
    source = REFERENCE.read_text().replace(KEYWORD_SEARCH, NEIGHBOUR_SEARCH)
    if bug == "wrong-first-match":
        source = source.replace(
            "Number(b.text === args.query) - Number(a.text === args.query)",
            "Number(a.text === args.query) - Number(b.text === args.query)",
        )
    else:
        source = source.replace(".slice(0, args.k);", ".slice(0, args.k + 1);")
    worker = tmp_path / "broken.mjs"
    worker.write_text(source)
    with pytest.raises(ProtocolError):
        run_conformance(
            shlex.join([NODE, str(worker)]),
            workdir=tmp_path / "run",
            visibility_timeout=0,
        )
