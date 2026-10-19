import sys

from memtorture.adapters.markdown import Markdown
from memtorture.protocol import Worker
from memtorture.suites import concurrent, inspect_overlap
from memtorture.types import Record, Scope


class HistoryMarkdown(Markdown):
    def capabilities(self):
        return super().capabilities() | {"history"}

    def upsert(self, scope, record):
        return self.write(scope, record)


def test_versions_are_not_duplicate_record_ids():
    scope = Scope("t", "s")
    first = Record.make(scope, "key", "first")
    second = Record.make(scope, "key", "second")
    expected = {r.id: r for r in (first, second)}
    counts = inspect_overlap(expected, [first, second], {"key": second})
    assert not any(counts.values())
    repeated = inspect_overlap(expected, [first, second, first], {"key": second})
    assert repeated["duplicates"] == 1


def test_history_store_passes_and_reports_versions_per_key(monkeypatch, tmp_path):
    class HistoryWorker(Worker):
        def __init__(self, system, config, workdir, **kwargs):
            super().__init__(
                system,
                config,
                workdir,
                command=[
                    sys.executable,
                    "-c",
                    "from tests.test_overlap_history import HistoryMarkdown; "
                    "from memtorture.worker import serve; serve(HistoryMarkdown())",
                ],
                **kwargs,
            )

    monkeypatch.setattr("memtorture.suites.Worker", HistoryWorker)
    result = concurrent(
        "concurrent-kv",
        "markdown",
        "append",
        tmp_path,
        writer_counts=(2,),
        writes=8,
    )
    assert result["status"] == "ok"
    assert result["duplicates"] == 0
    assert result["history"] == {
        "advertised": True,
        "max_versions_per_key": 4,
        "observations": [
            {
                "writers": 2,
                "versions_per_key": {f"shared-{i}": 4 for i in range(4)},
            }
        ],
    }
