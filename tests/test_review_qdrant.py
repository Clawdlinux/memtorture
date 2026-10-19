import sys
from types import SimpleNamespace
from unittest.mock import Mock

from memproof.adapters.qdrant import COLLECTION, Qdrant, point_id
from memproof.protocol import Worker
from memproof.suites import read_path
from memproof.types import Ack, Record, Ref, Scope
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
        "from memproof.worker import serve; serve(ReferenceProbe())",
    ]
    with Worker("probe", "default", tmp_path, command=command) as worker:
        record = Record.make(Scope("t", "s"), "key", "memory")
        ack = worker.call("write", scope=record.scope, record=record)
        assert worker.refs[record.id] == ack.ref == Ref("native-point-id")


def test_id_read_path_uses_one_native_retrieve():
    adapter = Qdrant()
    adapter.wait = True
    adapter.client = Mock()
    record = Record.make(Scope("t", "s"), "key", "memory")
    point = adapter._point(record)
    adapter.client.retrieve.side_effect = lambda collection, ids, **kwargs: (
        [SimpleNamespace(payload=point.payload)] if ids == [point.id] else []
    )
    adapter.client.scroll.return_value = (
        [SimpleNamespace(payload=point.payload)],
        None,
    )
    ack = adapter.write(record.scope, record)

    def call(op, **kwargs):
        return getattr(adapter, op)(**kwargs)

    worker = SimpleNamespace(
        call=call,
        capabilities=adapter.capabilities(),
        refs={record.id: getattr(ack, "ref", None)},
    )
    assert read_path(worker, "id", record) == [record]
    adapter.client.retrieve.assert_called_once_with(
        COLLECTION,
        [point_id(record.scope, record.key)],
        with_payload=True,
    )
    adapter.client.scroll.assert_not_called()
