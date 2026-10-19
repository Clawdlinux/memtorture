import contextlib
import io
import json
import time
from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from memtorture.conformance import search_matches
from memtorture.protocol import OPERATIONS, ProtocolError, decode, encode, parse_reply
from memtorture.types import Ack, NotSupported, Record, Ref, Scope
from memtorture.worker import serve
from tests.helpers import ROOT, Probe, make_record, matches, pid_exists, reply_command


@pytest.mark.parametrize("kind", ["record", ["record"], {"unexpected": "tag"}])
def test_wire_values(kind):
    record = make_record(text="é\u0301\n\t完整")
    value = encode(record)
    assert value["kind"] == "record"
    assert value["scope"] == {"tenant": "tenant", "session": "session"}
    value["future"] = {"kind": kind}
    value["scope"].update(kind=kind, future={"nested": {"kind": kind}})
    assert decode(json.loads(json.dumps(value))) == record
    assert decode({"future": {"kind": kind}}) == {"future": {"kind": kind}}
    assert encode(Ref("native")) == {"kind": "ref", "id": "native"}
    requests = [
        {"id": 1, "op": "start", "args": {"workdir": ".", "config": "default"}},
        {"id": 2, "op": "nope", "args": {}},
        {
            "id": 3,
            "op": "write",
            "future": 1,
            "args": {
                "scope": encode(record.scope),
                "record": value,
                "future": {"kind": "ack"},
            },
        },
        {
            "id": 4,
            "op": "get",
            "args": {
                "scope": encode(record.scope),
                "key": record.key,
            },
        },
    ]
    output = io.StringIO()
    serve(Probe(), io.StringIO("".join(json.dumps(r) + "\n" for r in requests)), output)
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[1]["error"]["type"] == "not_supported"
    assert replies[2]["result"]["id"] == record.id
    assert replies[-1]["result"]["text"] == record.text
    assert decode(replies[-1]["result"]) == record
    value["text"] += "corrupt"
    assert not decode(value).valid()


@given(st.text(), st.text(), st.text())
def test_record_hash(tenant, key, text):
    record = Record.make(Scope(tenant, "session"), key, text)
    assert record.valid()
    assert not replace(record, text=text + "\0").valid()


@pytest.mark.parametrize("outcome", ["read", "kill", "dropped"])
def test_ack_lifecycle(worker_factory, outcome):
    worker = worker_factory("dropped" if outcome == "dropped" else "default")
    record = make_record()
    assert worker.info["protocol"] == 1
    assert worker.info["servers"] == []
    assert isinstance(worker.info["notes"], str)
    token = worker.send("write", scope=record.scope, record=record)
    time.sleep(0.03)
    assert worker.acked == worker.refs == {}
    if outcome == "kill":
        worker.kill("worker")
        assert worker.acked == {}
        return
    ack = worker.receive(token)
    assert ack == Ack(
        record.id, dropped=outcome == "dropped", ref=Ref("native-point-id")
    )
    assert worker.acked == ({} if ack.dropped else {record.id: record})
    assert worker.refs == ({} if ack.dropped else {record.id: ack.ref})
    assert worker.call("get", scope=record.scope, key=record.key) == record
    with pytest.raises(NotSupported):
        worker.call("search", scope=record.scope, query="x", k=1)
    worker.close()
    assert worker.process.poll() == 0


@pytest.mark.parametrize(
    "raw",
    [
        b'{"id":2,"result":{"id":"r","dropped":false,"ref":null}}\n',
        b'{"id":2,"result":{"kind":"ack","id":"wrong","dropped":false,"ref":null}}\n',
        b"stray worker log\n",
        b"\xff\n",
    ],
)
def test_unvalidated_reply_is_not_acked(worker_factory, raw):
    worker = worker_factory(command=reply_command(raw))
    record = make_record(id="r")
    with pytest.raises(ProtocolError):
        worker.call("write", scope=record.scope, record=record)
    assert worker.acked == worker.refs == {}
    assert worker.last_ack_ns == 0


@pytest.mark.parametrize(
    "op,envelope",
    [
        ("write", {"result": {"id": "r", "dropped": False, "ref": None}}),
        (
            "write",
            {"result": {"kind": "ack", "id": "other", "dropped": True, "ref": None}},
        ),
        ("get", {"result": {"kind": "record", "id": "r"}}),
        ("read_recent", {"result": None}),
        ("stop", {"result": {}}),
        ("capabilities", {"result": ["write", 1]}),
        (
            "start",
            {
                "result": {
                    "protocol": True,
                    "capabilities": [],
                    "version": "",
                    "notes": "",
                    "servers": [],
                }
            },
        ),
        ("write", {"error": {"type": "ValueError", "message": "bad"}}),
        (
            "delete",
            {
                "result": {
                    "kind": "ack",
                    "id": "wrong",
                    "dropped": True,
                    "ref": None,
                }
            },
        ),
        ("delete", {"result": {"id": "native", "dropped": False, "ref": None}}),
    ],
)
def test_invalid_reply(op, envelope):
    with pytest.raises(ProtocolError):
        parse_reply(
            json.dumps({"id": 1, **envelope}).encode(),
            1,
            op,
            {"record": make_record(id="r"), "ref": Ref("native")},
        )


def test_wire_schemas():
    root = ROOT / "docs/protocol/v1"
    records, covered = {}, set()

    def expand(value):
        if isinstance(value, str) and value.startswith("$record:"):
            return records[value.removeprefix("$record:")]
        if isinstance(value, list):
            return [expand(v) for v in value]
        if isinstance(value, dict):
            return {k: expand(v) for k, v in value.items()}
        return value

    for line in (root / "golden.jsonl").read_text().splitlines():
        case = json.loads(line)
        request = expand(case["request"])
        args = request["args"]
        for record in [args["record"]] if "record" in args else args.get("records", []):
            records[record["id"]] = record
        reply = expand(case["reply"])
        path = root / f"{request['op']}.json"
        schema = json.loads(path.read_text())
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$defs"]["request"]["properties"]["op"] == {
            "const": request["op"]
        }
        assert schema["anyOf"] == [
            {"$ref": "#/$defs/request"},
            {"$ref": "#/$defs/reply"},
        ]
        for message, direction in ((request, "request"), (reply, "reply")):
            assert matches(schema, message, path), (request["op"], message)
            assert matches(schema["$defs"][direction], message, path)
        assert matches(schema, request | reply, path)
        covered.add(request["op"])
    assert covered == OPERATIONS
    assert matches(
        json.loads((root / "common.json").read_text()), {}, root / "common.json"
    )


@pytest.mark.parametrize(
    "mode",
    [
        "malformed",
        "error",
        "timeout",
        "nonzero",
        "preserve",
        "stop-object",
        "trailing-log",
        "partial-log",
        "extra-reply",
    ],
)
def test_shutdown(worker_factory, mode):
    if mode in {"stop-object", "trailing-log", "partial-log", "extra-reply"}:
        trailing = {
            "stop-object": b"",
            "trailing-log": b"late log\n",
            "partial-log": b"partial log",
            "extra-reply": b'{"id":3,"result":null}\n',
        }[mode]
        result = b"{}" if mode == "stop-object" else b"null"
        worker = worker_factory(
            command=reply_command(
                b'{"id":2,"result":' + result + b"}\n",
                stop=True,
                trailing=trailing,
            )
        )
    else:
        worker = worker_factory(
            "malformed" if mode == "preserve" else mode,
            name="ServerProbe",
        )
    if mode == "timeout":
        worker.timeout = 0.1
    expected = (
        pytest.raises(ProtocolError)
        if mode
        in {"malformed", "stop-object", "trailing-log", "partial-log", "extra-reply"}
        else contextlib.nullcontext()
    )
    with expected:
        worker.close(preserve_server=mode == "preserve")
    assert worker.process.poll() is not None
    assert worker.log.closed
    assert worker.process.stdout.closed
    assert all(
        pid_exists(pid) == (mode == "preserve") for pid in worker.servers.values()
    )


@pytest.mark.parametrize("target", ["server", "server:api", "server:storage", "both"])
def test_server_fault(worker_factory, target):
    worker = worker_factory(name="ServerProbe")
    original = dict(worker.servers)
    worker.kill(target)
    for role, pid in original.items():
        killed = target in {"server", "both", f"server:{role}"}
        assert pid_exists(pid) is not killed
    worker.close()
    assert not any(pid_exists(pid) for pid in original.values())
    restarted = worker_factory(name="ServerProbe")
    assert set(restarted.servers) == set(original)
    assert all(pid_exists(pid) for pid in restarted.servers.values())
    restarted.close()
    assert not any(pid_exists(pid) for pid in restarted.servers.values())


def test_search_contract():
    first, second = make_record("one", "first"), make_record("two", "second")
    with pytest.raises(ProtocolError, match="duplicate"):
        search_matches(
            [second, second, first],
            {"scope": first.scope, "query": second.text, "k": 3},
            first=second,
        )
