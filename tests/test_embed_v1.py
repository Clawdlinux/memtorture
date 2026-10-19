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
    assert embed("memory")[:4] == [
        0.023604549455747994,
        0.1878642582255064,
        0.14056290482726713,
        0.12370505655101338,
    ]


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
        assert (
            hashlib.sha256(b"\0\0\0\0" + case["text"].encode()).hexdigest()
            == case["first_digest"]
        )
        expected = json.dumps(case["vector"], separators=(",", ":"), allow_nan=False)
        diagnostic = max(
            abs(a - b) for a, b in zip(vector, case["vector"], strict=True)
        )
        assert json.dumps(vector, separators=(",", ":"), allow_nan=False) == expected, (
            f"Maximum component difference: {diagnostic}; diagnostic tolerance: 1e-15"
        )
        assert output["text"] == case["text"]
        assert (
            json.dumps(output["vector"], separators=(",", ":"), allow_nan=False)
            == expected
        )


def test_vectors_cover_four_byte_utf8_and_multiple_sha256_blocks():
    cases = json.loads(VECTORS.read_text())["vectors"]
    assert any(any(ord(c) > 0xFFFF for c in case["text"]) for case in cases)
    assert any(len(case["text"].encode("utf-8")) > 64 for case in cases)


@pytest.mark.parametrize("text", ["\ud800", "\udfff", "before\ud800after", "x\udc00"])
def test_python_rejects_lone_surrogates(text):
    with pytest.raises(UnicodeError):
        embed(text)


@pytest.mark.skipif(NODE is None, reason="Node is missing")
@pytest.mark.parametrize("text", ["\ud800", "\udfff", "before\ud800after", "x\udc00"])
def test_node_rejects_lone_surrogates(text):
    result = subprocess.run(
        [NODE, "systems/reference-node/embed.mjs"],
        input=json.dumps([text]),
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "Unicode scalar" in result.stderr


@pytest.mark.skipif(NODE is None, reason="Node is missing")
def test_one_ulp_difference_fails_even_below_diagnostic_threshold(monkeypatch):
    original = subprocess.run

    def perturb(*args, **kwargs):
        result = original(*args, **kwargs)
        outputs = json.loads(result.stdout)
        value = outputs[0]["vector"][0]
        outputs[0]["vector"][0] = math.nextafter(value, math.inf)
        assert abs(outputs[0]["vector"][0] - value) < 1e-15
        result.stdout = json.dumps(outputs)
        return result

    monkeypatch.setattr(subprocess, "run", perturb)
    with pytest.raises(AssertionError):
        test_published_vectors_match_both_languages()


def test_signed_word_boundary_maps_int32_min_to_negative_one(monkeypatch):
    from types import SimpleNamespace

    digest = b"\x80\0\0\0" + bytes(28)
    monkeypatch.setattr(
        hashlib, "sha256", lambda _: SimpleNamespace(digest=lambda: digest)
    )
    assert embed("synthetic boundary") == [
        -1 / math.sqrt(8) if i % 8 == 0 else 0.0 for i in range(64)
    ]


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
