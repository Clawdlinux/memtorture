import hashlib
import json
import math
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from memtorture.controls import gate
from memtorture.embed import DIMENSIONS, embed

NODE = shutil.which("node")
VECTORS = Path("docs/protocol/v1/embed-vectors.json")


def test_embed_matches_independent_sha256_vector():
    assert embed("memory")[:4] == pytest.approx(
        [
            0.023604549455747994,
            0.1878642582255064,
            0.14056290482726713,
            0.12370505655101338,
        ],
        rel=0,
        abs=1e-15,
    )


@pytest.mark.skipif(NODE is None, reason="Node is missing")
def test_published_vectors_match_both_languages():
    fixtures = json.loads(VECTORS.read_text())
    texts = [case["text"] for case in fixtures["vectors"]]
    process = subprocess.run(
        [NODE, "systems/reference-node/embed.mjs"],
        input=json.dumps(texts, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=True,
    )
    outputs = json.loads(process.stdout)
    for case, output in zip(fixtures["vectors"], outputs, strict=True):
        vector = embed(case["text"])
        assert len(vector) == DIMENSIONS == 64
        assert all(math.isfinite(v) for v in vector)
        assert sum(v * v for v in vector) == pytest.approx(1)
        assert (
            hashlib.sha256(b"\0\0\0\0" + case["text"].encode()).hexdigest()
            == case["first_digest"]
        )
        assert vector == pytest.approx(case["vector"], rel=0, abs=1e-15)
        assert output["text"] == case["text"]
        assert output["vector"] == pytest.approx(case["vector"], rel=0, abs=1e-15)


def test_embed_preserves_exact_utf8_input():
    assert embed("é") != embed("e\u0301")
    assert embed("memory") != embed("memory\n")
    assert embed("") != embed("\0")


def test_documented_vectors_match_machine_readable_vectors():
    blocks = re.findall(
        r"```json\n(.*?)\n```", Path("docs/PROTOCOL.md").read_text(), re.S
    )
    documented = [json.loads(block) for block in blocks if '"first_digest"' in block]
    assert documented == json.loads(VECTORS.read_text())["vectors"]


@pytest.mark.parametrize(
    "relative",
    [
        "docs/PROTOCOL.md",
        "docs/protocol/v1/embed-vectors.json",
        "systems/sqlite/manifest.json",
        "systems/reference-node/worker.mjs",
    ],
)
def test_gate_hash_covers_protocol_and_worker_contract(tmp_path, monkeypatch, relative):
    module = tmp_path / "memtorture/controls/gate.py"
    module.parent.mkdir(parents=True)
    module.write_text("")
    monkeypatch.setattr(gate, "__file__", str(module))
    before = gate.methodology_hash()
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed contract\n")
    assert gate.methodology_hash() != before
