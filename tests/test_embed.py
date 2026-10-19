import hashlib
import json
import math
import subprocess

import pytest

from memtorture.embed import DIMENSIONS, embed
from tests.helpers import ROOT

VECTORS = json.loads((ROOT / "docs/protocol/v1/embed-vectors.json").read_text())[
    "vectors"
]


@pytest.mark.parametrize(
    "case", VECTORS, ids=[f"vector-{i}" for i in range(len(VECTORS))]
)
def test_published_vectors(node, case):
    process = subprocess.run(
        [node, str(ROOT / "systems/reference-node/embed.mjs")],
        input=json.dumps([case["text"]], ensure_ascii=False),
        text=True,
        capture_output=True,
        check=True,
    )
    (output,) = json.loads(process.stdout)
    vector = embed(case["text"])
    assert len(vector) == DIMENSIONS == 64
    assert all(math.isfinite(v) for v in vector)
    assert sum(x * x for x in vector) == pytest.approx(1)
    assert vector == embed(case["text"])
    assert (
        hashlib.sha256(b"\0\0\0\0" + case["text"].encode()).hexdigest()
        == case["first_digest"]
    )
    expected = json.dumps(case["vector"], separators=(",", ":"), allow_nan=False)
    assert json.dumps(vector, separators=(",", ":"), allow_nan=False) == expected
    assert output["text"] == case["text"]
    assert (
        json.dumps(output["vector"], separators=(",", ":"), allow_nan=False) == expected
    )


@pytest.mark.parametrize("language", ["python", "node"])
@pytest.mark.parametrize("text", ["\ud800", "\udfff", "before\ud800after", "x\udc00"])
def test_surrogate_rejection(request, language, text):
    if language == "python":
        with pytest.raises(UnicodeError):
            embed(text)
    else:
        result = subprocess.run(
            [
                request.getfixturevalue("node"),
                str(ROOT / "systems/reference-node/embed.mjs"),
            ],
            input=json.dumps([text]),
            text=True,
            capture_output=True,
        )
        assert result.returncode != 0
        assert "Unicode scalar" in result.stderr


@pytest.mark.parametrize(
    "left,right",
    [
        ("é", "e\u0301"),
        ("memory", "memory\n"),
        ("", "\0"),
        ("first", "second"),
    ],
)
def test_input_bytes(left, right):
    assert embed(left) != embed(right)
