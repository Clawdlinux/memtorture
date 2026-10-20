import json

import pytest

from memtorture.suites import (
    forget,
    forget_residue,
    recovery,
    supersede_by_key,
    write_throughput,
)
from memtorture.types import NotSupported


@pytest.mark.parametrize(
    "system,missing", [("sqlite-busy", False), ("md-rmw-sleep", True)]
)
def test_throughput_keeps_loss_visible(tmp_path, system, missing):
    events = []
    result = write_throughput(
        system,
        "default",
        tmp_path,
        writer_counts=(1, 8),
        writes=20,
        timeout_s=0,
        emit=events.append,
    )
    assert result["status"] == "ok"
    assert result["control_gate"]["gated"] is False
    assert (result["acked_missing"] > 0) is missing
    assert [trial["writers"] for trial in result["trials"]] == [1, 8]
    for trial in result["trials"]:
        assert trial["acked"] == trial["writers"] * 20
        assert trial["ops_per_second"] > 0
        latency = trial["write_latency_ms"]
        assert latency["n"] == trial["attempted"]
        assert latency["p50"] > 0
        if latency["n"] < 100:
            assert latency["p99"] is None
        else:
            assert latency["p99"] >= latency["p50"]
        starts = [
            e
            for e in events
            if e["operation"] == "worker-start" and e["writers"] == trial["writers"]
        ]
        assert len(starts) == trial["writers"]
        assert trial["start_ns"] >= max(e["start_reply_ns"] for e in starts)
        assert all(e["spawn_ns"] <= e["start_reply_ns"] for e in starts)
        assert trial["window_ms"] == pytest.approx(
            (trial["end_ns"] - trial["start_ns"]) / 1e6
        )
        writes = [
            e
            for e in events
            if e["operation"] == "write" and e["writers"] == trial["writers"]
        ]
        assert len(writes) == trial["acked"]
        assert {e["text_bytes"] for e in writes} == {128, 1024, 8192}
        assert trial["start_ns"] <= min(e["start_ns"] for e in writes)
        assert trial["end_ns"] == max(e["ack_ns"] for e in writes)
        assert trial["ops_per_second"] == pytest.approx(
            trial["acked"] * 1e9 / (trial["end_ns"] - trial["start_ns"])
        )
    assert result["trials"][0]["acked_missing"] == 0


@pytest.mark.parametrize(
    "system,expected", [("append-oldest", "violation"), ("sqlite-upsert", "ok")]
)
def test_supersession_suite(tmp_path, system, expected):
    result = supersede_by_key(system, "default", tmp_path)
    assert result["status"] == expected
    assert result["versions"] == 5
    assert result["acked"] == 10 and result["dropped"] == 0
    assert result["info"]["version"]


@pytest.mark.parametrize(
    "system,expected",
    [
        ("sqlite-forget-fts", "violation"),
        ("sqlite-forget-both", "ok"),
        ("sqlite-delete-delayed", "ok"),
    ],
)
def test_delete_checks_every_read_path(tmp_path, system, expected):
    events = []
    result = forget(
        system, "default", tmp_path, timeout_s=0.2, poll_ms=1, emit=events.append
    )
    assert result["status"] == expected
    assert result["forget_promise"]["kind"] == "delete"
    assert result["forget_promise"]["url"] and result["forget_promise"]["quote"]
    assert set(result["paths"]) == {
        "get",
        "get_by_ref",
        "list_prefix",
        "read_recent",
        "search",
    }
    for path, detail in result["paths"].items():
        stale = system == "sqlite-forget-fts" and path == "search"
        assert detail["status"] == ("violation" if stale else "ok")
        assert detail["visible_before"] is True
        assert detail["remaining"] == int(stale)
        assert (detail["lag_ms"] is None) is stale
    if system == "sqlite-delete-delayed":
        assert max(detail["lag_ms"] for detail in result["paths"].values()) >= 50
    assert len([e for e in events if e["operation"] == "forget-check"]) == 5


@pytest.mark.parametrize(
    "system,expected",
    [("sqlite-residue-off", "violation"), ("sqlite-residue-secure", "ok")],
)
def test_residue_is_measurable_before_delete(tmp_path, system, expected):
    events = []
    result = forget_residue(system, "default", tmp_path, emit=events.append)
    assert result["status"] == expected
    assert result["occurrences_before"] > 0
    assert (result["occurrences_after"] > 0) == (expected == "violation")
    assert result["bytes_found"] == 32 * result["occurrences_after"]
    assert result["forget"]["status"] == "ok"
    assert result["scan_after_stop"] is True
    assert "record" not in json.dumps(events)
    assert all("canary" not in event for event in events)


@pytest.mark.parametrize("config", ["default", "missing", "overreport"])
def test_clean_recovery_reports_counts_and_rss(tmp_path, recovery_worker, config):
    events = []
    result = recovery(
        "sqlite-busy",
        config,
        tmp_path,
        sizes=(25, 10025),
        samples=12,
        emit=events.append,
    )
    assert result["status"] == "ok"
    assert result["bulk_load_timed"] is False
    assert result["control_gate"]["gated"] is False
    assert set(result["sizes"]) == {"25", "10025"}
    assert result["open_ms_source"] == "self-reported"
    anomaly = "open_ms exceeds spawn_to_start_ms" if config == "overreport" else None
    assert result["timing_anomaly"] == anomaly
    for size, metric in result["sizes"].items():
        assert metric["spawn_to_start_ms"] > 0
        assert metric["start_to_first_read_ms"] > 0
        assert metric["timing_anomaly"] == anomaly
        if config == "missing":
            assert metric["open_ms"] is None
            assert metric["unattributed_start_ms"] is None
            assert metric["recovery_ms"] is None
            assert "open_ms" not in result["info"]
        else:
            assert metric["open_ms"] >= 0
            assert metric["unattributed_start_ms"] == pytest.approx(
                metric["spawn_to_start_ms"] - metric["open_ms"]
            )
            if config == "overreport":
                assert metric["unattributed_start_ms"] < 0
                assert metric["recovery_ms"] is None
            else:
                assert metric["unattributed_start_ms"] >= 0
                assert metric["recovery_ms"] == pytest.approx(
                    metric["open_ms"] + metric["start_to_first_read_ms"]
                )
                assert metric["time_to_ready_ms"] >= metric["recovery_ms"]
        assert metric["time_to_ready_ms"] == pytest.approx(
            metric["spawn_to_start_ms"] + metric["start_to_first_read_ms"]
        )
        assert (
            metric["verification"]["enumeration_pages"] == (int(size) + 9999) // 10000
        )
        assert metric["rss_bytes"]["worker"] > 0
        assert metric["verification"]["count"] == int(size)
        assert metric["verification"]["samples_checked"] == 12
        assert metric["verification"]["samples_missing"] == 0
        assert metric["verification"]["count_matches"] is True
    assert result["sizes"]["10025"]["open_ms"] == result["info"].get("open_ms")
    assert len([e for e in events if e["operation"] == "recovery-check"]) == 2


@pytest.mark.parametrize("config", ["encoded", "no-get", "unreadable-search"])
def test_encoded_residue_is_not_a_false_pass(encoded_worker, tmp_path, config):
    events = []
    result = forget_residue("markdown", "encoded", tmp_path, emit=events.append)
    assert result["status"] == "not measurable"
    assert result["occurrences_before"] == 0
    assert "occurrences_after" not in result and "forget" not in result
    assert [e["operation"] for e in events] == ["residue-check"]
    deletion = forget(
        "markdown",
        config,
        tmp_path / "delete",
        timeout_s=0.01,
        poll_ms=1,
        emit=events.append,
    )
    assert deletion["status"] == (
        "not measurable" if config == "unreadable-search" else "ok"
    )
    assert all(
        detail["status"]
        == (
            "N/A"
            if config == "no-get" and path == "get"
            else "not measurable"
            if config == "unreadable-search" and path == "search"
            else "ok"
        )
        for path, detail in deletion["paths"].items()
    )


@pytest.mark.parametrize(
    "body,config",
    [
        (supersede_by_key, "no-upsert"),
        (supersede_by_key, "no-get"),
        (forget, "no-delete"),
        (forget, "no-ref"),
        (write_throughput, "no-list"),
        (recovery, "no-get"),
    ],
)
def test_missing_native_operations_are_na(encoded_worker, tmp_path, body, config):
    with pytest.raises(NotSupported):
        body("markdown", config, tmp_path)


@pytest.mark.parametrize(
    "config,expected", [("recovery-loss", "violation"), ("no-list", "ok")]
)
def test_recovery_reports_incomplete_readback(
    encoded_worker, tmp_path, config, expected
):
    result = recovery("markdown", config, tmp_path, sizes=(20,), samples=5)
    assert result["status"] == expected
    verification = result["sizes"]["20"]["verification"]
    assert verification["samples_checked"] == 5
    if config == "recovery-loss":
        assert verification["count"] == 19
        assert verification["count_matches"] is False
        assert verification["samples_missing"] == 1
    else:
        assert verification["count"] is verification["count_matches"] is None
        assert verification["count_source"] == "N/A"
        assert verification["samples_missing"] == 0


@pytest.mark.slow
@pytest.mark.parametrize(
    "body,options", [(forget, {}), (recovery, {"sizes": (30,), "samples": 5})]
)
def test_native_async_delete_and_server_recovery(tmp_path, available, body, options):
    available("qdrant")
    result = body("qdrant", "wait=false", tmp_path, **options)
    assert result["status"] == "ok"
    if body is forget:
        assert all(path["status"] == "ok" for path in result["paths"].values())
        assert "v-1-16-x" in result["forget_promise"]["url"]
    else:
        metric = result["sizes"]["30"]
        assert set(metric["rss_bytes"]) == {"storage"}
        assert metric["rss_bytes"]["storage"] > 0
        assert metric["verification"]["count"] == 30
        assert metric["verification"]["samples_missing"] == 0
