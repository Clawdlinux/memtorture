from dataclasses import replace

from memproof.adapters.markdown import Markdown
from memproof.protocol import Worker
from memproof.suites import concurrent, inspect_records
from memproof.types import Record, Scope


def test_torn_markdown_rows_survive_prefix_and_limit(tmp_path):
    adapter = Markdown()
    adapter.start(tmp_path, "append")
    record = Record.make(Scope("t", "s"), "writer0", "memory")
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


def test_concurrent_readback_counts_torn_lines(monkeypatch, tmp_path):
    class TornWorker(Worker):
        injected = False

        def call(self, op, **kwargs):
            if op == "list_prefix" and not self.injected:
                type(self).injected = True
                with (self.workdir / "memory.md").open("a") as output:
                    output.write("torn first\n{torn second\n")
            return super().call(op, **kwargs)

    monkeypatch.setattr("memproof.suites.Worker", TornWorker)
    result = concurrent(
        "concurrent-append",
        "markdown",
        "append",
        tmp_path,
        writer_counts=(2,),
        writes=2,
    )
    assert result["torn"] == 2
    assert result["duplicates"] == result["acked_missing"] == 0
    assert result["status"] == "violation"


def test_overlap_checks_duplicates_and_every_returned_row():
    from memproof.suites import inspect_overlap

    scope = Scope("t", "s")
    first = Record.make(scope, "shared", "first")
    second = Record.make(scope, "shared", "second")
    unknown = Record.make(scope, "unknown", "not acked")
    changed = replace(Record.make(scope, "other", "original"), text="changed")
    expected = {r.id: r for r in (first, second)}
    counts = inspect_overlap(
        expected, [first, second, unknown, changed], {"shared": second}
    )
    assert counts["duplicates"] == 1
    assert counts["invalid_rows"] == 2
    assert counts["torn"] == 1
    assert counts["invalid_final"] == 0


def test_overlap_phase_uses_returned_rows(monkeypatch, tmp_path):
    class DuplicateWorker(Worker):
        def call(self, op, **kwargs):
            rows = super().call(op, **kwargs)
            if op == "list_prefix" and kwargs["prefix"] == "shared-":
                return rows + [rows[0], Record.make(rows[0].scope, "extra", "unknown")]
            return rows

    monkeypatch.setattr("memproof.suites.Worker", DuplicateWorker)
    result = concurrent(
        "concurrent-kv",
        "sqlite-busy",
        "default",
        tmp_path,
        writer_counts=(2,),
        writes=2,
    )
    assert result["duplicates"] == 1
    assert result["invalid_rows"] == 1
    assert result["status"] == "violation"


def test_capabilities_are_not_false_measurements(tmp_path):
    result = concurrent(
        "concurrent-kv",
        "sqlite-busy",
        "default",
        tmp_path,
        writer_counts=(2,),
        writes=2,
    )
    assert result["cas"] == result["history"] == "not measured in B1"
