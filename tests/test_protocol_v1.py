import io
import json
from pathlib import Path

import pytest

from memtorture.protocol import Worker, decode, encode
from memtorture.types import Record, Ref, Scope
from memtorture.worker import serve
from tests.test_protocol import Probe


def test_v1_uses_language_neutral_values():
    scope = Scope("tenant", "session")
    record = Record.make(scope, "key", "café\n完整")
    value = encode(record)
    assert value["kind"] == "record"
    assert value["scope"] == {"tenant": "tenant", "session": "session"}
    assert "_type" not in json.dumps(value)
    value["future"] = True
    value["scope"]["future"] = 1
    assert decode(value) == record
    assert encode(Ref("native-id")) == {"kind": "ref", "id": "native-id"}


def test_start_negotiates_version_and_server_roles():
    source = io.StringIO(
        '{"id":1,"op":"start","args":{"workdir":".","config":"default"}}\n'
    )
    output = io.StringIO()
    serve(Probe(), source, output)
    result = json.loads(output.getvalue())["result"]
    assert result["protocol"] == 1
    assert result["servers"] == []
    assert isinstance(result["notes"], str)


@pytest.mark.parametrize(
    "system,config", [("markdown", "append"), ("sqlite", "synchronous=NORMAL")]
)
def test_full_record_upsert_and_native_ref_roundtrip(tmp_path, system, config):
    scope = Scope("t", "s")
    record = Record.make(scope, "key", "unchanged café\ntext")
    with Worker(system, config, tmp_path) as worker:
        ack = worker.call("upsert", scope=scope, record=record)
        assert ack.id == record.id
        assert ack.ref is not None
        assert worker.call("get_by_ref", scope=scope, ref=ack.ref) == record
        deleted = worker.call("delete", scope=scope, ref=ack.ref)
        assert deleted.id == ack.ref.id
        assert worker.call("get_by_ref", scope=scope, ref=ack.ref) is None


def test_manifest_selects_worker_command():
    import memtorture.protocol as protocol

    assert callable(protocol.worker_command)
    command = protocol.worker_command("sqlite")
    manifest = Path("systems/sqlite/manifest.json")
    assert manifest.is_file()
    assert command[-1] == "sqlite"


@pytest.mark.parametrize(
    "op,result",
    [
        ("write", {"id": "r", "dropped": False, "ref": None}),
        ("write", {"kind": "ack", "id": "other", "dropped": True, "ref": None}),
        ("get", {"kind": "record", "id": "r"}),
        ("read_recent", None),
        ("stop", {}),
        ("capabilities", ["write", 1]),
        (
            "start",
            {
                "protocol": True,
                "capabilities": [],
                "version": "",
                "notes": "",
                "servers": [],
            },
        ),
    ],
)
def test_malformed_replies_are_protocol_errors(op, result):
    from memtorture.protocol import ProtocolError, validate_reply

    record = Record.make(Scope("t", "s"), "k", "v", id="r")
    with pytest.raises(ProtocolError):
        validate_reply({"id": 1, "result": result}, 1, op, {"record": record})


def test_unknown_error_types_are_rejected():
    from memtorture.protocol import ProtocolError, validate_reply

    with pytest.raises(ProtocolError):
        validate_reply(
            {"id": 1, "error": {"type": "ValueError", "message": "bad"}},
            1,
            "write",
            {},
        )


def test_protocol_schemas_cover_every_operation():
    from memtorture.protocol import OPERATIONS

    root = Path("docs/protocol/v1")
    for op in OPERATIONS:
        schema = json.loads((root / f"{op}.json").read_text())
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$defs"]["request"]["properties"]["op"] == {"const": op}
        # Unknown request fields may also look like a reply envelope.
        assert schema["anyOf"] == [
            {"$ref": "#/$defs/request"},
            {"$ref": "#/$defs/reply"},
        ]


def test_qdrant_delete_uses_native_point_ref_and_preserves_scope():
    from memtorture.adapters.qdrant import Qdrant

    adapter = Qdrant()
    adapter.wait = True
    scope = Scope("t", "s")
    record = Record.make(scope, "key", "text", id="record-id")
    calls = []

    def points(op, body, wait=None, method="POST"):
        calls.append((op, body, wait))
        if op == "":
            return [
                {
                    "payload": {
                        "id": record.id,
                        "tenant": scope.tenant,
                        "session": scope.session,
                        "key": record.key,
                        "text": record.text,
                        "payload_sha256": record.payload_sha256,
                    }
                }
            ]
        return {}

    adapter._points = points
    ref = Ref("native-point-not-record-id")
    assert adapter.delete(scope, ref).id == ref.id
    assert calls[-1] == ("/delete", {"points": [ref.id]}, True)
    calls.clear()
    adapter.delete(Scope("other", "s"), ref)
    assert all(op != "/delete" for op, _, _ in calls)


def test_python_worker_ignores_extra_fields_and_keeps_text():
    scope = Scope("t", "s")
    record = Record.make(scope, "key", "é\u0301\n\t完整")
    value = encode(record)
    value["future"] = {"kind": "record", "opaque": True}
    value["scope"]["future"] = 123
    requests = [
        {"id": 1, "op": "start", "args": {"workdir": ".", "config": "default"}},
        {
            "id": 2,
            "op": "write",
            "future": 1,
            "args": {
                "scope": encode(scope),
                "record": value,
                "future": {"kind": "ack"},
            },
        },
        {"id": 3, "op": "get", "args": {"scope": encode(scope), "key": "key"}},
    ]
    output = io.StringIO()
    serve(Probe(), io.StringIO("".join(json.dumps(r) + "\n" for r in requests)), output)
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[-1]["result"]["text"] == record.text
    assert decode(replies[-1]["result"]) == record


def test_faults_are_targeted_by_server_role(monkeypatch):
    from unittest.mock import Mock

    worker = object.__new__(Worker)
    worker.servers = {"api": 123, "storage": 456}
    worker.server_pid = 456
    worker.process = Mock()
    worker.process.poll.return_value = 0
    worker._wait_server_exit = Mock()
    killed = []
    monkeypatch.setattr(
        "memtorture.protocol.os.kill", lambda pid, sig: killed.append(pid)
    )
    worker.kill("server:api")
    assert killed == [123]
    worker._wait_server_exit.assert_called_once_with(123)
