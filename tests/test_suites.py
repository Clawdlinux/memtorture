import json
import time
from types import SimpleNamespace

import pytest

from memproof.controls.gate import control_gate, require_controls
from memproof.embed import embed
from memproof.protocol import Worker
from memproof.suites import concurrent, history_scale, process_kill, recall_delay
from memproof.types import NotSupported


def test_embed_is_seeded_and_normalized():
    assert embed("memory") == embed("memory")
    assert embed("memory", seed=1) != embed("memory", seed=2)
    assert sum(x * x for x in embed("memory")) == pytest.approx(1)


def test_recall_paths(tmp_path):
    result = recall_delay("sqlite-visible", "default", tmp_path, samples=1)
    assert result["status"] == "ok"
    assert set(result["paths"]) == {"id", "filter", "recency", "search"}
    for path in result["paths"].values():
        assert path["timeouts"] == 0
        assert path["dropped"] == 0
        assert path["visible"] == 1
        assert path["time_to_visible_ms"]["p50"] >= path["write_latency_ms"]["p50"]


@pytest.mark.parametrize(
    "system,expected", [("buffered", "violation"), ("sqlite-off", "ok")]
)
def test_kill_controls(system, expected, tmp_path):
    result = process_kill(system, "default", tmp_path, trials=2, seed=10)
    assert result["status"] == expected
    assert result["torn"] == 0


@pytest.mark.parametrize("suite", ["concurrent-append", "concurrent-kv"])
@pytest.mark.parametrize(
    "system,expected", [("md-rmw-sleep", "violation"), ("sqlite-busy", "ok")]
)
def test_concurrency_controls(suite, system, expected, tmp_path):
    result = concurrent(
        suite, system, "default", tmp_path, writer_counts=(2,), writes=6
    )
    assert result["status"] == expected
    assert result["torn"] == 0


def test_history_bulk_and_curves(tmp_path):
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
    assert len([e for e in events if e["operation"] == "history-read"]) == 12


def test_gate_refuses_missing_or_failed_controls(tmp_path):
    with pytest.raises(NotSupported, match="controls"):
        require_controls("process-kill", tmp_path)
    path = tmp_path / "gate.json"
    path.write_text(json.dumps({"suites": {"process-kill": {"discriminates": False}}}))
    with pytest.raises(NotSupported, match="controls"):
        require_controls("process-kill", tmp_path)


def test_control_gate_threshold_is_not_rounded():
    assert not control_gate(8, 10, 10)
    assert control_gate(9, 10, 10)
    assert not control_gate(10, 9, 10)


def test_kill_has_no_post_ack_sleep(monkeypatch, tmp_path):
    def forbidden_sleep(seconds):
        raise AssertionError("A post-ack sleep can hide a short flush window")

    monkeypatch.setattr(
        "memproof.suites.time",
        SimpleNamespace(monotonic_ns=time.monotonic_ns, sleep=forbidden_sleep),
    )
    assert process_kill("sqlite-off", "default", tmp_path, trials=1)["status"] == "ok"


def test_fault_precedes_write_log_io(monkeypatch, tmp_path):
    workers = []

    class TrackingWorker(Worker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.faulted = False
            workers.append(self)

        def kill(self, target):
            super().kill(target)
            self.faulted = True

    def emit(data):
        if data["operation"] == "write":
            assert workers[-1].faulted, "Log IO must not hide a flush window"

    monkeypatch.setattr("memproof.suites.Worker", TrackingWorker)
    process_kill("sqlite-off", "default", tmp_path, trials=1, emit=emit)


def test_recall_poll_timing_excludes_result_log_io(tmp_path):
    def slow_log(data):
        if data["operation"] == "poll":
            time.sleep(0.03)

    result = recall_delay(
        "delayed-index",
        "default",
        tmp_path,
        samples=1,
        poll_ms=1,
        paths=("id",),
        emit=slow_log,
    )
    metric = result["paths"]["id"]
    assert metric["visibility_lag_ms"]["p50"] <= 101 + metric["read_ms"]["p99"]
