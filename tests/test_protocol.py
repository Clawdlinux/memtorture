import io
import json
import sys
import time

import pytest

from memtorture.protocol import Worker, decode, encode
from memtorture.results import ResultWriter
from memtorture.types import Ack, Record, Scope
from memtorture.worker import serve


class Probe:
    name = "probe"

    def start(self, workdir, config):
        self.records = {}

    def capabilities(self):
        return {"write", "get"}

    def write(self, scope, record):
        self.records[record.key] = record
        return Ack(record.id)

    def get(self, scope, key):
        return self.records.get(key)

    def stop(self, target="all", kill=False):
        pass


def probe_command():
    return [
        sys.executable,
        "-c",
        "from tests.test_protocol import Probe; "
        "from memtorture.worker import serve; serve(Probe())",
    ]


def test_wire_roundtrip():
    record = Record.make(Scope("tenant", "session"), "key", "café")
    assert decode(json.loads(json.dumps(encode(record)))) == record
    assert record.valid()


def test_hash_rejects_flipped_byte():
    record = Record.make(Scope("t", "s"), "k", "original")
    value = encode(record)
    value["text"] = "originaL"
    assert not decode(value).valid()


def test_ack_requires_read_reply(tmp_path):
    with Worker("probe", "default", tmp_path, command=probe_command()) as worker:
        record = Record.make(Scope("t", "s"), "k", "v")
        token = worker.send("write", scope=record.scope, record=record)
        time.sleep(0.03)
        assert worker.acked == {}
        assert worker.receive(token) == Ack(record.id)
        assert worker.acked == {record.id: record}
        assert worker.call("get", scope=record.scope, key="k") == record


def test_unread_reply_never_enters_ack_set(tmp_path):
    worker = Worker("probe", "default", tmp_path, command=probe_command())
    record = Record.make(Scope("t", "s"), "k", "v")
    worker.send("write", scope=record.scope, record=record)
    time.sleep(0.02)
    worker.kill("worker")
    assert not worker.acked
    worker.close()


def test_worker_reports_unsupported(tmp_path):
    with Worker("probe", "default", tmp_path, command=probe_command()) as worker:
        from memtorture.types import NotSupported

        with pytest.raises(NotSupported):
            worker.call("search", scope=Scope("t", "s"), query="x", k=1)


def test_worker_survives_bad_request():
    source = io.StringIO('{"id":1,"op":"nope","args":{}}\n')
    output = io.StringIO()
    serve(Probe(), source, output)
    assert json.loads(output.getvalue())["error"]["type"] == "not_supported"


def test_result_manifest_and_alphabetical_table(tmp_path):
    with ResultWriter(tmp_path, command=["memtorture", "test"]) as writer:
        writer.operation({"suite": "x", "system": "z", "latency_ms": 1})
        writer.cell({"suite": "x", "system": "z", "config": "default"})
        writer.cell({"suite": "x", "system": "a", "config": "default"})
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["harness_commit"]
    assert manifest["hardware"]["machine"]
    assert manifest["command"] == ["memtorture", "test"]
    table = (tmp_path / "x.md").read_text()
    assert table.index("| a |") < table.index("| z |")
    assert json.loads((tmp_path / "operations.jsonl").read_text())["latency_ms"] == 1
