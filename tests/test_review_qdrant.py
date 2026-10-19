import sys
from types import SimpleNamespace

from memtorture.adapters.qdrant import COLLECTION, Qdrant, point_id
from memtorture.protocol import Worker
from memtorture.suites import read_path
from memtorture.types import Ack, Record, Ref, Scope
from tests.test_protocol import Probe


class ReferenceProbe(Probe):
    def write(self, scope, record):
        super().write(scope, record)
        return Ack(record.id, ref=Ref("native-point-id"))


def test_worker_preserves_native_write_reference(tmp_path):
    command = [
        sys.executable,
        "-c",
        "from tests.test_review_qdrant import ReferenceProbe; "
        "from memtorture.worker import serve; serve(ReferenceProbe())",
    ]
    with Worker("probe", "default", tmp_path, command=command) as worker:
        record = Record.make(Scope("t", "s"), "key", "memory")
        ack = worker.call("write", scope=record.scope, record=record)
        assert worker.refs[record.id] == ack.ref == Ref("native-point-id")


def test_id_read_path_uses_one_native_retrieve():
    adapter = Qdrant()
    adapter.wait = True
    record = Record.make(Scope("t", "s"), "key", "memory")
    point = adapter._point(record)
    calls = []

    def call(method, path, body=None):
        calls.append((method, path, body))
        if path.endswith("/points?wait=true"):
            return {"result": {"status": "completed"}}
        if path == f"/collections/{COLLECTION}/points":
            hit = body["ids"] == [point["id"]]
            return {"result": [{"payload": point["payload"]}] if hit else []}
        raise AssertionError(f"unexpected call {method} {path}")

    adapter.client = SimpleNamespace(call=call)
    ack = adapter.write(record.scope, record)
    calls.clear()

    def op(name, **kwargs):
        return getattr(adapter, name)(**kwargs)

    worker = SimpleNamespace(
        call=op,
        capabilities=adapter.capabilities(),
        refs={record.id: getattr(ack, "ref", None)},
    )
    assert read_path(worker, "id", record) == [record]
    assert calls == [
        (
            "POST",
            f"/collections/{COLLECTION}/points",
            {"ids": [point_id(record.scope, record.key)], "with_payload": True},
        )
    ]


def test_write_uses_put_with_wait_flag():
    for wait, flag in ((True, "true"), (False, "false")):
        adapter = Qdrant()
        adapter.wait = wait
        calls = []

        def call(method, path, body=None, calls=calls):
            calls.append((method, path))
            return {"result": {}}

        adapter.client = SimpleNamespace(call=call)
        record = Record.make(Scope("t", "s"), "key", "memory")
        adapter.write(record.scope, record)
        assert calls == [("PUT", f"/collections/{COLLECTION}/points?wait={flag}")]
