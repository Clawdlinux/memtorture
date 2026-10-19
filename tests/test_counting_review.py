from dataclasses import replace

import pytest

from memtorture.protocol import Worker
from memtorture.suites import concurrent, inspect_overlap, inspect_records
from memtorture.types import Record, Scope


@pytest.mark.parametrize("copies", [1, 2])
@pytest.mark.parametrize("recompute_digest", [False, True])
def test_corrupt_copies_are_torn_not_duplicates(copies, recompute_digest):
    record = Record.make(Scope("t", "s"), "key", "original")
    corrupt = replace(record, text="corrupt")
    if recompute_digest:
        corrupt = replace(corrupt, payload_sha256=corrupt.digest())
    expected = {record.id: record}
    rows = [record] * copies + [corrupt]
    for counts in (
        inspect_records(expected, rows),
        inspect_overlap(expected, rows, {"key": record}),
    ):
        assert counts["duplicates"] == copies - 1
        assert counts["torn"] == 1


@pytest.mark.parametrize("corrupt_first", [False, True])
def test_versions_keep_the_valid_row_regardless_of_corrupt_copy_order(
    tmp_path, monkeypatch, corrupt_first
):
    class CorruptCopyWorker(Worker):
        def call(self, op, **kwargs):
            result = super().call(op, **kwargs)
            if op == "list_prefix" and kwargs["prefix"] == "shared-":
                corrupt = replace(result[0], text="corrupt")
                return [corrupt, *result] if corrupt_first else [*result, corrupt]
            return result

    monkeypatch.setattr("memtorture.suites.Worker", CorruptCopyWorker)
    result = concurrent(
        "concurrent-kv",
        "sqlite-busy",
        "default",
        tmp_path,
        writer_counts=(2,),
        writes=1,
    )
    assert result["history"]["max_versions_per_key"] == 1
    assert result["history"]["observations"][0]["versions_per_key"] == {"shared-0": 1}
    assert result["duplicates"] == 0
    assert result["torn"] == 1
    assert result["status"] == "violation"
