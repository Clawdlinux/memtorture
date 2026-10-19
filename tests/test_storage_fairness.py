import json

import pytest

from memtorture.adapters import load_adapter
from memtorture.adapters.qdrant import Qdrant
from memtorture.types import Record, Scope

MARKDOWN_ROWS = [
    ("markdown", "append"),
    ("markdown", "rewrite"),
    ("md-rmw-sleep", "default"),
    ("buffered", "default"),
]
SQLITE_ROWS = [
    ("sqlite", "synchronous=NORMAL"),
    ("sqlite", "synchronous=FULL"),
    ("sqlite-off", "default"),
    ("sqlite-busy", "default"),
    ("sqlite-visible", "default"),
    ("delayed-index", "default"),
]


def storage_record():
    return Record.make(
        Scope("tenant", "session"),
        "path/file",
        "needle0001 text",
        id="00000000-0000-0000-0000-000000000001",
    )


@pytest.mark.parametrize("name,config", MARKDOWN_ROWS)
@pytest.mark.parametrize("operation", ["write", "bulk_load", "upsert"])
def test_markdown_and_controls_store_compact_records_without_vectors(
    tmp_path, name, config, operation
):
    adapter = load_adapter(name)
    adapter.start(tmp_path, config)
    record = storage_record()
    try:
        if operation == "bulk_load":
            adapter.bulk_load(record.scope, [record])
        else:
            getattr(adapter, operation)(record.scope, record)
    finally:
        adapter.stop()
    line = (tmp_path / "memory.md").read_bytes()
    assert len(line.splitlines()) == 1
    assert len(line) <= 256
    stored = json.loads(line.decode().removeprefix("- "))
    assert "vector" not in stored
    assert Record.from_value(record.scope, record.key, json.dumps(stored)) == record
    assert record.vector is not None and len(record.vector) == 64


@pytest.mark.parametrize("name,config", SQLITE_ROWS)
@pytest.mark.parametrize("operation", ["write", "bulk_load", "upsert"])
def test_sqlite_and_controls_do_not_persist_unused_vectors(
    tmp_path, name, config, operation
):
    adapter = load_adapter(name)
    adapter.start(tmp_path, config)
    record = storage_record()
    try:
        if operation == "bulk_load":
            adapter.bulk_load(record.scope, [record])
        else:
            getattr(adapter, operation)(record.scope, record)
        assert "vector" not in {
            row[1] for row in adapter.db.execute("PRAGMA table_info(records)")
        }
        assert adapter.db.execute(
            "SELECT id,tenant,session,path,text,payload_sha256 FROM records"
        ).fetchall() == [
            (
                record.id,
                record.scope.tenant,
                record.scope.session,
                record.key,
                record.text,
                record.payload_sha256,
            )
        ]
        assert adapter.db.execute("SELECT text FROM fts").fetchall() == [(record.text,)]
    finally:
        adapter.stop()


def test_vector_store_keeps_the_harness_vector_outside_the_payload():
    record = storage_record()
    point = Qdrant._point(record)
    assert point["vector"] == record.vector
    assert "vector" not in point["payload"]


def test_compact_value_does_not_copy_an_unused_vector():
    class UnusedVector(list):
        def __iter__(self):
            raise AssertionError(
                "Non-vector storage must not traverse vector components"
            )

    original = storage_record()
    record = Record(
        original.id,
        original.scope,
        original.key,
        original.text,
        original.payload_sha256,
        UnusedVector([0.0] * 64),
    )
    assert "vector" not in json.loads(record.value())


@pytest.mark.parametrize(
    "name", sorted({name for name, _ in MARKDOWN_ROWS + SQLITE_ROWS} | {"qdrant"})
)
def test_adapters_do_not_advertise_the_legacy_id_capability(name):
    assert "get-id" not in load_adapter(name).capabilities()
