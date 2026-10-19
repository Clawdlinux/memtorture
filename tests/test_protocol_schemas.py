import json
from pathlib import Path

from tests.schema_subset import matches

ROOT = Path(__file__).resolve().parents[1] / "docs/protocol/v1"


def test_every_schema_validates_golden_requests_and_replies():
    records = {}
    covered = set()

    def expand(value):
        if isinstance(value, str) and value.startswith("$record:"):
            return records[value.removeprefix("$record:")]
        if isinstance(value, list):
            return [expand(v) for v in value]
        if isinstance(value, dict):
            return {k: expand(v) for k, v in value.items()}
        return value

    for line in (ROOT / "golden.jsonl").read_text().splitlines():
        case = json.loads(line)
        request = expand(case["request"])
        args = request["args"]
        for record in [args["record"]] if "record" in args else args.get("records", []):
            records[record["id"]] = record
        reply = expand(case["reply"])
        path = ROOT / f"{request['op']}.json"
        schema = json.loads(path.read_text())
        for message, direction in ((request, "request"), (reply, "reply")):
            assert matches(schema, message, path), (request["op"], message)
            assert matches(schema["$defs"][direction], message, path)
        overlapping = request | reply
        assert matches(schema, overlapping, path)
        broken_union = {"oneOf": schema["anyOf"]}
        assert not matches(broken_union, overlapping, path)
        covered.add(path.name)
    schema_files = {
        p.name
        for p in ROOT.glob("*.json")
        if "$schema" in json.loads(p.read_text()) and p.name != "common.json"
    }
    assert covered == schema_files
    common_path = ROOT / "common.json"
    common = json.loads(common_path.read_text())
    assert matches(common, {}, common_path)


def test_subset_checks_types_required_fields_and_unknown_fields():
    path = ROOT / "write.json"
    schema = json.loads(path.read_text())["$defs"]["reply"]
    valid = {
        "id": 1,
        "result": {
            "kind": "ack",
            "id": "r",
            "dropped": False,
            "ref": None,
            "future": {"kind": ["unknown"]},
        },
    }
    assert matches(schema, valid, path)
    assert not matches(schema, valid | {"id": True}, path)
    assert not matches(schema, {"id": 1, "result": {"kind": "ack"}}, path)
    assert not matches(schema, valid | {"error": {"type": "bad", "message": ""}}, path)
