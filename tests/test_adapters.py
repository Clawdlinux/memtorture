import json
import time
from types import SimpleNamespace

import pytest

from memtorture.adapters import load_adapter
from memtorture.adapters.qdrant import COLLECTION, Qdrant, point_id
from memtorture.suites import inspect_records, read_path
from memtorture.types import NotSupported, Record, Ref, Scope
from tests.helpers import LOCAL_ROWS, QdrantClient, make_record


@pytest.mark.parametrize("name,config", LOCAL_ROWS)
def test_storage_roundtrip(adapter_factory, tmp_path, name, config):
    adapter = adapter_factory(name, config)
    first = make_record()
    scope, other = first.scope, Scope("other", "session")
    ack = adapter.write(scope, first)
    assert ack.id == first.id
    if name == "delayed-index":
        assert adapter.get(scope, first.key) is None
    if name in {"buffered", "delayed-index"}:
        time.sleep(0.13)
    assert adapter.get(scope, first.key) == first
    assert adapter.get_by_ref(scope, ack.ref) == first
    assert adapter.get_by_ref(other, ack.ref) is None
    assert adapter.get(other, first.key) is None
    assert adapter.read_recent(scope, 20) == [first]
    assert adapter.list_prefix(scope, "path/", 20) == [first]
    assert adapter.search(scope, "needle0001", 20) == [first]
    assert adapter.search(other, "needle0001", 20) == []
    second = make_record("path/bulk", "bulk text")
    before = adapter.path.read_bytes() if hasattr(adapter, "path") else None
    adapter.bulk_load(scope, [second])
    if before is not None:
        assert adapter.path.read_bytes().startswith(before)
    replacement = make_record(first.key, "replacement café\ntext")
    updated = adapter.upsert(scope, replacement)
    assert updated.id == replacement.id
    if name == "delayed-index":
        time.sleep(0.13)
    assert adapter.get(scope, first.key) == replacement
    assert adapter.get_by_ref(scope, updated.ref) == replacement
    assert {r.id for r in adapter.list_prefix(scope, "path/", 20)} == {
        second.id,
        replacement.id,
    }
    if hasattr(adapter, "db"):
        assert "vector" not in {
            row[1] for row in adapter.db.execute("PRAGMA table_info(records)")
        }
        assert adapter.db.execute(
            "SELECT id,tenant,session,path,text,payload_sha256 "
            "FROM records ORDER BY path"
        ).fetchall() == [
            (r.id, scope.tenant, scope.session, r.key, r.text, r.payload_sha256)
            for r in sorted((second, replacement), key=lambda r: r.key)
        ]
        assert sorted(adapter.db.execute("SELECT text FROM fts").fetchall()) == sorted(
            [(r.text,) for r in (second, replacement)]
        )
    else:
        lines = adapter.path.read_bytes().splitlines()
        assert len(lines) == 2
        stored = [json.loads(line.decode().removeprefix("- ")) for line in lines]
        written = [
            json.loads(line.decode().removeprefix("- ")) for line in before.splitlines()
        ]
        assert all("vector" not in row for row in [*written, *stored])
        assert max(map(len, lines)) <= 256
        assert {
            Record.from_value(scope, row["key"], json.dumps(row)).id for row in stored
        } == {
            second.id,
            replacement.id,
        }
    assert first.vector is not None and len(first.vector) == 64
    assert adapter.data_dirs() == [tmp_path]
    with pytest.raises(NotSupported):
        adapter.retrieve_for(None)
    adapter.delete(other, updated.ref)
    assert adapter.get_by_ref(scope, updated.ref) == replacement
    adapter.delete(scope, updated.ref)
    assert adapter.get_by_ref(scope, updated.ref) is None


@pytest.mark.parametrize("name", ["markdown", "sqlite"])
def test_invalid_config(name, tmp_path):
    with pytest.raises(NotSupported):
        load_adapter(name).start(tmp_path, "invalid-row")


@pytest.mark.parametrize(
    "name,config", [("sqlite", "synchronous=NORMAL"), ("sqlite-upsert", "default")]
)
def test_sqlite_mappings(adapter_factory, name, config):
    adapter = adapter_factory(name, config)
    record = make_record("path/file")
    adapter.write(record.scope, record)
    indexes = {
        tuple(
            row[2] for row in adapter.db.execute(f"PRAGMA index_info('{index[1]}')")
        ): index[1]
        for index in adapter.db.execute("PRAGMA index_list(records)")
        if index[2]
    }
    statements = []
    adapter.db.set_trace_callback(statements.append)
    assert adapter.get(record.scope, record.key) == record
    assert adapter.list_prefix(record.scope, "path/", 20) == [record]
    assert adapter.get_by_ref(record.scope, Ref(record.id)) == record
    adapter.db.set_trace_callback(None)
    selects = [sql for sql in statements if sql.startswith("SELECT")]
    assert len(selects) == 3
    for sql, columns in zip(
        selects,
        [("tenant", "session", "path")] * 2 + [("id",)],
        strict=True,
    ):
        plan = " ".join(
            row[3] for row in adapter.db.execute("EXPLAIN QUERY PLAN " + sql)
        )
        assert indexes[columns] in plan
        assert "scope_seq" not in plan and "ORDER BY" not in sql
    assert "path >=" in selects[1] and "path <" in selects[1]
    keyed = make_record("collision", "older keyed value")
    by_id = make_record("another-key", "newer id value", id="collision")
    foreign = make_record("path/foreign", scope=Scope("other", "session"))
    for row in (keyed, by_id, foreign):
        adapter.write(row.scope, row)
    assert adapter.get(keyed.scope, "collision") == keyed
    assert adapter.get_by_ref(keyed.scope, Ref("collision")) == by_id
    assert adapter.get(foreign.scope, by_id.id) is None
    assert adapter.list_prefix(record.scope, "path/", 20) == [record]
    original_seq = adapter.db.execute(
        "SELECT seq FROM records WHERE id=?", (keyed.id,)
    ).fetchone()
    replacement = make_record(keyed.key, "replacementtoken")
    ack = adapter.upsert(keyed.scope, replacement)
    assert ack.id == replacement.id
    assert adapter.get(keyed.scope, keyed.key) == replacement
    assert adapter.get_by_ref(keyed.scope, Ref(keyed.id)) is None
    assert adapter.get_by_ref(keyed.scope, ack.ref) == replacement
    assert adapter.get(foreign.scope, foreign.key) == foreign
    assert adapter.search(keyed.scope, keyed.text, 20) == []
    assert adapter.search(keyed.scope, replacement.text, 20) == [replacement]
    if name == "sqlite-upsert":
        assert (
            adapter.db.execute(
                "SELECT seq FROM records WHERE id=?", (replacement.id,)
            ).fetchone()
            == original_seq
        )


def test_torn_storage(adapter_factory):
    adapter = adapter_factory("markdown", "append")
    record = make_record("writer0")
    adapter.write(record.scope, record)
    with adapter.path.open("a") as output:
        output.write("torn first\n\n{torn second\n")
    rows = adapter.list_prefix(record.scope, "writer", 1)
    assert {r.id for r in rows if not r.valid()} == {"malformed:2", "malformed:4"}
    assert inspect_records({record.id: record}, rows) == {
        "acked_missing": 0,
        "duplicates": 0,
        "torn": 2,
    }


@pytest.mark.parametrize("wait,sequence", [(False, 0), (True, 42)])
def test_qdrant_requests(wait, sequence):
    record = make_record()
    point = {
        "id": point_id(record.scope, record.key),
        "payload": {
            "id": record.id,
            "tenant": record.scope.tenant,
            "session": record.scope.session,
            "key": record.key,
            "text": record.text,
            "payload_sha256": record.payload_sha256,
        },
    }
    adapter = Qdrant()
    adapter.wait = wait
    adapter.client = client = QdrantClient(point)
    sequenced = SimpleNamespace(**vars(record), sequence=sequence)
    ack = adapter.write(record.scope, sequenced)
    assert ack.ref == Ref(point["id"])
    assert len(client.calls) == 1
    method, path, body = client.calls[0]
    assert (method, path) == (
        "PUT",
        f"/collections/{COLLECTION}/points?wait={str(wait).lower()}",
    )
    assert body["points"][0]["vector"] == record.vector
    assert "vector" not in body["points"][0]["payload"]
    assert body["points"][0]["payload"]["_order"] == sequence
    client.calls.clear()
    worker = SimpleNamespace(
        call=lambda op, **kwargs: getattr(adapter, op)(**kwargs),
        capabilities=adapter.capabilities(),
        refs={record.id: ack.ref},
    )
    assert read_path(worker, "id", record) == [record]
    assert client.calls == [
        (
            "POST",
            f"/collections/{COLLECTION}/points",
            {"ids": [point["id"]], "with_payload": True},
        )
    ]
    assert adapter.get_by_ref(Scope("other", "session"), ack.ref) is None
    client.calls.clear()
    assert adapter.search(record.scope, "text", 0, vector=record.vector) == []
    assert client.calls == []
    client.visible = False
    adapter.write(record.scope, sequenced)
    deleted = adapter.delete(record.scope, ack.ref)
    assert deleted.id == ack.ref.id
    condition = [
        {"key": "tenant", "match": {"value": "tenant"}},
        {"key": "session", "match": {"value": "session"}},
        {"has_id": [ack.ref.id]},
    ]
    assert len(client.calls) == 2
    assert client.calls[-1] == (
        "POST",
        f"/collections/{COLLECTION}/points/delete?wait={str(wait).lower()}",
        {"filter": {"must": condition}},
    )
    queued = {}
    for _, path, body in client.calls:
        if "/delete" in path:
            queued.pop(body["filter"]["must"][-1]["has_id"][0], None)
        else:
            queued.update((p["id"], p) for p in body["points"])
    assert ack.ref.id not in queued
    adapter.delete(Scope("other", "session"), ack.ref)
    assert client.calls[-1][2]["filter"]["must"] == [
        {"key": "tenant", "match": {"value": "other"}},
        *condition[1:],
    ]
