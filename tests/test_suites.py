import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from memtorture.suites import (
    concurrent,
    history_scale,
    inspect_overlap,
    inspect_records,
    process_kill,
    read_path,
    recall_delay,
)
from memtorture.types import NotSupported
from tests.helpers import (
    HistoryWorker,
    ProbeWorker,
    ReadbackWorker,
    TimingWorker,
    make_record,
)


@pytest.mark.parametrize(
    "system,config",
    [
        ("sqlite-visible", "default"),
        ("delayed-index", "default"),
    ],
)
def test_recall_paths(tmp_path, system, config):
    slow_poll_logged = False

    def emit(data):
        nonlocal slow_poll_logged
        if (
            system == "delayed-index"
            and data["operation"] == "poll"
            and not slow_poll_logged
        ):
            slow_poll_logged = True
            time.sleep(0.15)

    result = recall_delay(
        system,
        config,
        tmp_path,
        samples=1,
        paths=("id",) if system == "delayed-index" else None,
        poll_ms=1 if system == "delayed-index" else 10,
        emit=emit,
    )
    assert result["status"] == "ok"
    assert slow_poll_logged == (system == "delayed-index")
    assert result["torn"] == 0
    assert set(result["paths"]) == (
        {"id"} if system == "delayed-index" else {"id", "filter", "recency", "search"}
    )
    for metric in result["paths"].values():
        assert metric["timeouts"] == metric["dropped"] == 0
        assert metric["visible"] == 1
        assert metric["time_to_visible_ms"]["p50"] >= metric["write_latency_ms"]["p50"]
        if system == "delayed-index":
            # The fault adds 100 ms of delay, plus scheduling headroom.
            assert metric["visibility_lag_ms"]["p50"] < 130


@pytest.mark.parametrize(
    "system,config,expected",
    [
        ("buffered", "default", "violation"),
        ("sqlite-off", "default", "ok"),
        ("qdrant", "wait=true", "ok"),
    ],
)
def test_kill_recovery(tmp_path, available, system, config, expected):
    available(system)
    events = []
    trials = 1 if system == "qdrant" else 2
    result = process_kill(
        system, config, tmp_path, trials=trials, seed=10, emit=events.append
    )
    assert result["status"] == expected
    assert result["torn"] == result["duplicates"] == 0
    assert (result["acked_missing"] > 0) == (expected == "violation")
    targets = {"worker", "server:storage", "both"} if system == "qdrant" else {"worker"}
    assert {trial["target"] for trial in result["trials"]} == targets
    assert len(result["trials"]) == len(targets) * trials
    writes = [e for e in events if e["operation"] == "write"]
    assert sum(t["acked"] for t in result["trials"]) == len(writes)
    assert all(t["acked"] > 0 for t in result["trials"])
    assert sum(t["acked_missing"] for t in result["trials"]) == result["acked_missing"]
    for trial in result["trials"]:
        same_trial = [
            e
            for e in writes
            if (e["trial"], e["target"]) == (trial["trial"], trial["target"])
        ]
        assert all(e["ack_ns"] <= trial["fault_ns"] for e in same_trial)


@pytest.mark.parametrize("suite", ["concurrent-append", "concurrent-kv"])
@pytest.mark.parametrize(
    "system,config,expected",
    [
        ("md-rmw-sleep", "default", "violation"),
        ("sqlite-busy", "default", "ok"),
    ],
)
def test_concurrent_counts(tmp_path, suite, system, config, expected):
    events = []
    result = concurrent(
        suite,
        system,
        config,
        tmp_path,
        writer_counts=(2,),
        writes=6,
        emit=events.append,
    )
    assert result["status"] == expected
    assert result["torn"] == result["duplicates"] == 0
    assert (result["acked_missing"] > 0) == (expected == "violation")
    checks = [e for e in events if e["operation"] == "concurrent-check"]
    assert len(checks) == (2 if suite == "concurrent-kv" else 1)
    assert sum(t["acked_missing"] for t in result["trials"]) == result["acked_missing"]
    for phase in ("distinct", "overlap") if suite == "concurrent-kv" else ("distinct",):
        writes = [
            e
            for e in events
            if e.get("phase") == phase and e["operation"] in {"write", "upsert"}
        ]
        assert len(writes) == 12
        assert not any(e["dropped"] for e in writes)
    assert result["cas"] == "not measured in B1"
    assert result["history"]["advertised"] is False
    if suite == "concurrent-kv" and expected == "ok":
        assert result["history"]["max_versions_per_key"] == 1


def test_history_curves(tmp_path):
    events = []
    result = history_scale(
        "sqlite",
        "synchronous=NORMAL",
        tmp_path,
        sizes=(10, 25),
        repeats=2,
        emit=events.append,
    )
    assert result["status"] == "ok"
    assert set(result["sizes"]) == {"10", "25"}
    assert result["bulk_load_timed"] is False
    assert len([e for e in events if e["operation"] == "history-read"]) == 12
    assert result["control_gate"]["gated"] is False
    for size in result["sizes"].values():
        for metric in size.values():
            assert metric["latency_ms"]["n"] == 2
            assert metric["latency_ms"]["p99"] is None


@pytest.mark.parametrize("copies", [1, 2])
@pytest.mark.parametrize("recompute_digest", [False, True])
def test_record_counts(copies, recompute_digest):
    first = make_record("shared", "first")
    second = make_record("shared", "second")
    corrupt = replace(first, text="corrupt")
    if recompute_digest:
        corrupt = replace(corrupt, payload_sha256=corrupt.digest())
    expected = {r.id: r for r in (first, second)}
    rows = [first] * copies + [second, corrupt]
    for counts in (
        inspect_records(expected, rows),
        inspect_overlap(expected, rows, {"shared": second}),
    ):
        assert counts["duplicates"] == copies - 1
        assert counts["torn"] == 1
        assert counts["acked_missing"] == 0
    unknown = make_record("unknown", "not acked")
    overlap = inspect_overlap(
        expected, [first, second, unknown, corrupt], {"shared": second}
    )
    assert overlap == {
        "acked_missing": 0,
        "duplicates": 0,
        "torn": 1,
        "invalid_rows": 2,
        "invalid_final": 0,
    }
    assert inspect_records(expected, [first])["acked_missing"] == 1


@pytest.mark.parametrize(
    "fault", ["torn", "duplicate", "corrupt-first", "corrupt-last"]
)
def test_concurrent_readback(tmp_path, monkeypatch, fault):
    monkeypatch.setattr(ReadbackWorker, "fault", fault)
    monkeypatch.setattr("memtorture.suites.Worker", ReadbackWorker)
    suite = "concurrent-append" if fault == "torn" else "concurrent-kv"
    system, config = (
        ("markdown", "append") if fault == "torn" else ("sqlite-busy", "default")
    )
    result = concurrent(suite, system, config, tmp_path, writer_counts=(2,), writes=2)
    assert result["status"] == "violation"
    assert result["acked_missing"] == 0
    assert result["duplicates"] == (1 if fault == "duplicate" else 0)
    assert result["torn"] == (
        0 if fault == "duplicate" else 2 if fault == "torn" else 1
    )
    if fault == "duplicate":
        assert result["invalid_rows"] == 1
    if fault.startswith("corrupt"):
        assert result["history"]["max_versions_per_key"] == 1
        assert result["history"]["observations"][0]["versions_per_key"] == {
            "shared-0": 1,
            "shared-1": 1,
        }


@pytest.mark.parametrize("elsewhere", [False, True])
def test_history_versions(monkeypatch, tmp_path, elsewhere):
    if elsewhere:
        monkeypatch.chdir(tmp_path)
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


@pytest.mark.parametrize("config", ["no-capability", "no-ref"])
def test_unavailable_id_path(worker_factory, monkeypatch, tmp_path, config):
    worker = worker_factory(config)
    record = make_record()
    worker.call("write", scope=record.scope, record=record)
    with pytest.raises(NotSupported):
        read_path(worker, "id", record)
    worker.close()
    monkeypatch.setattr("memtorture.suites.Worker", ProbeWorker)
    result = recall_delay(
        "probe", config, tmp_path / "recall", paths=("id",), samples=1
    )
    assert result["paths"]["id"]["status"] == "N/A"


def test_fault_timing(monkeypatch, tmp_path):
    monkeypatch.setattr(TimingWorker, "instances", [])
    monkeypatch.setattr("memtorture.suites.Worker", TimingWorker)

    def forbidden_sleep(seconds):
        raise AssertionError("A post-ack sleep can hide buffering")

    def emit(data):
        if data["operation"] == "write":
            assert TimingWorker.instances[-1].faulted, "Fault must precede log IO"

    monkeypatch.setattr(
        "memtorture.suites.time",
        SimpleNamespace(monotonic_ns=time.monotonic_ns, sleep=forbidden_sleep),
    )
    assert (
        process_kill("sqlite-off", "default", tmp_path, trials=1, emit=emit)["status"]
        == "ok"
    )
