from memproof.adapters.sqlite import SQLite
from memproof.types import Record, Scope


def test_point_and_prefix_queries_use_unique_indexes(tmp_path):
    adapter = SQLite()
    adapter.start(tmp_path, "synchronous=NORMAL")
    try:
        scope = Scope("t", "s")
        record = Record.make(scope, "path/file", "memory")
        adapter.write(scope, record)
        indexes = {
            tuple(
                row[2] for row in adapter.db.execute(f"PRAGMA index_info('{index[1]}')")
            ): index[1]
            for index in adapter.db.execute("PRAGMA index_list(records)")
            if index[2]
        }
        statements = []
        adapter.db.set_trace_callback(statements.append)
        assert adapter.get(scope, record.key) == record
        assert adapter.list_prefix(scope, "path/", 20) == [record]
        assert adapter.get(scope, record.id) == record
        adapter.db.set_trace_callback(None)
        selects = [sql for sql in statements if sql.startswith("SELECT")]
        assert len(selects) == 4
        for sql, columns in zip(
            selects,
            [("tenant", "session", "path")] * 3 + [("id",)],
            strict=True,
        ):
            plan = " ".join(
                row[3] for row in adapter.db.execute("EXPLAIN QUERY PLAN " + sql)
            )
            assert indexes[columns] in plan, (sql, plan)
            assert "scope_seq" not in plan
            assert "ORDER BY" not in sql
        assert "path >=" in selects[1] and "path <" in selects[1]
    finally:
        adapter.stop()


def test_key_lookup_precedes_id_fallback_and_preserves_scope(tmp_path):
    adapter = SQLite()
    adapter.start(tmp_path, "synchronous=NORMAL")
    try:
        scope = Scope("t", "s")
        keyed = Record.make(scope, "collision", "older keyed value")
        by_id = Record.make(scope, "another-key", "newer id value", id="collision")
        adapter.write(scope, keyed)
        adapter.write(scope, by_id)
        assert adapter.get(scope, "collision") == keyed
        assert adapter.get(Scope("other", "s"), by_id.id) is None
    finally:
        adapter.stop()
