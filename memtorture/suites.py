import math
import random
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import asdict

from memtorture.protocol import Worker
from memtorture.types import NotSupported, Record, Scope

SUITES = (
    "recall-delay",
    "history-scale",
    "process-kill",
    "concurrent-append",
    "concurrent-kv",
)
HISTORY_CONTROLS = {
    "discriminates": None,
    "gated": False,
    "controls": "none defined in approved spec; measurement suite",
}
PATHS = {
    "id": "get_by_ref",
    "filter": "list_prefix",
    "recency": "read_recent",
    "search": "search",
}
PROCESS_NOTE = (
    "Tests user-space buffering and recovery, not disk durability. "
    "Worker-only faults test little for server-backed systems."
)


def percentiles(values):
    values = sorted(values)
    if not values:
        return {"p50": None, "p99": None}
    return {
        label: values[max(0, math.ceil(len(values) * q) - 1)]
        for label, q in (("p50", 0.5), ("p99", 0.99))
    }


def event(emit, **data):
    if emit:
        emit({"monotonic_ns": time.monotonic_ns(), **data})


def read_path(worker, path, record, limit=20):
    scope = record.scope
    if path == "id":
        if "get_by_ref" not in worker.capabilities:
            raise NotSupported("Missing get_by_ref")
        ref = worker.refs.get(record.id)
        if ref is None:
            raise NotSupported("Write returned no native ref")
        found = worker.call("get_by_ref", scope=scope, ref=ref)
        return [found] if found else []
    if path == "filter":
        return worker.call("list_prefix", scope=scope, prefix=record.key, limit=limit)
    if path == "recency":
        return worker.call("read_recent", scope=scope, n=limit)
    return worker.call("search", scope=scope, query=record.text, k=limit)


def recall_delay(
    system,
    config,
    workdir,
    *,
    samples=20,
    poll_ms=10,
    timeout_s=60,
    paths=None,
    emit=None,
):
    scope = Scope("benchmark", "recall-delay")
    metrics, violations = {}, 0
    with Worker(system, config, workdir) as worker:
        info = worker.info
        for path, capability in PATHS.items():
            if paths is not None and path not in paths:
                continue
            if capability not in worker.capabilities:
                metrics[path] = {"status": "N/A", "reason": f"Missing {capability}"}
                continue
            observations, read_latencies = [], []
            for index in range(samples):
                buffered_events = []
                event_sink = buffered_events.append if emit else None
                token = f"needle{path}{index:08d}"
                record = Record.make(scope, token, token)
                ack = worker.call("write", scope=scope, record=record)
                start, ack_ns = worker.last_sent_ns, worker.last_ack_ns
                write_ms = (ack_ns - start) / 1e6
                event(
                    event_sink,
                    operation="write",
                    path=path,
                    record=asdict(record),
                    dropped=ack.dropped,
                    write_latency_ms=write_ms,
                    start_ns=start,
                    ack_ns=ack_ns,
                )
                if ack.dropped:
                    observations.append({"status": "dropped", "write_ms": write_ms})
                    for data in buffered_events:
                        emit(data)
                    continue
                if path == "id" and record.id not in worker.refs:
                    metrics[path] = {
                        "status": "N/A",
                        "reason": "Write returned no native ref",
                    }
                    for data in buffered_events:
                        emit(data)
                    break
                deadline = ack_ns + int(timeout_s * 1e9)
                next_poll, polls = ack_ns, 0
                while True:
                    now = time.monotonic_ns()
                    if now >= deadline:
                        observations.append({"status": "timeout", "write_ms": write_ms})
                        break
                    if now < next_poll:
                        if poll_ms <= 1:
                            # Short calibration polls must not measure timer coalescing.
                            while time.monotonic_ns() < next_poll:
                                pass
                        else:
                            time.sleep((next_poll - now) / 1e9)
                    read_start = time.monotonic_ns()
                    rows = read_path(worker, path, record)
                    read_end = time.monotonic_ns()
                    polls += 1
                    read_ms = (read_end - read_start) / 1e6
                    read_latencies.append(read_ms)
                    torn = sum(not r.valid() for r in rows)
                    violations += torn
                    visible = any(r == record for r in rows)
                    event(
                        event_sink,
                        operation="poll",
                        path=path,
                        record_id=record.id,
                        start_ns=read_start,
                        end_ns=read_end,
                        read_ms=read_ms,
                        visible=visible,
                        torn=torn,
                    )
                    if visible:
                        observations.append(
                            {
                                "status": "visible",
                                "write_ms": write_ms,
                                "lag_ms": (read_end - ack_ns) / 1e6,
                                "total_ms": (read_end - start) / 1e6,
                                "polls": polls,
                            }
                        )
                        break
                    next_poll += int(poll_ms * 1e6)
                    if next_poll < read_end:
                        next_poll = read_end
                for data in buffered_events:
                    emit(data)
            if metrics.get(path, {}).get("status") == "N/A":
                continue
            visible = [o for o in observations if o["status"] == "visible"]
            metrics[path] = {
                "status": "measured",
                "visible": len(visible),
                "timeouts": sum(o["status"] == "timeout" for o in observations),
                "dropped": sum(o["status"] == "dropped" for o in observations),
                "write_latency_ms": percentiles([o["write_ms"] for o in observations]),
                "visibility_lag_ms": percentiles([o["lag_ms"] for o in visible]),
                "time_to_visible_ms": percentiles([o["total_ms"] for o in visible]),
                "read_ms": percentiles(read_latencies),
                "observations": observations,
            }
    return {
        "status": "violation" if violations else "ok",
        "paths": metrics,
        "torn": violations,
        "poll_ms": poll_ms,
        "timeout_s": timeout_s,
        "info": info,
    }


def inspect_records(expected, rows, *, keyed=False):
    by_id = {r.id: r for r in rows}
    counts = Counter(r.id for r in rows)
    return {
        "acked_missing": sum(r.id not in by_id for r in expected.values()),
        "duplicates": sum(max(0, count - 1) for count in counts.values()),
        "torn": sum(
            not r.valid() or (r.id in expected and r != expected[r.id]) for r in rows
        ),
    }


def inspect_overlap(expected, rows, final):
    counts = Counter(r.id for r in rows)
    return {
        "acked_missing": 0,
        "duplicates": sum(max(0, count - 1) for count in counts.values()),
        "torn": sum(
            not r.valid() or (r.id in expected and r != expected[r.id]) for r in rows
        ),
        "invalid_rows": sum(r.id not in expected or r != expected[r.id] for r in rows),
        "invalid_final": sum(
            not r or r.id not in expected or r != expected[r.id] for r in final.values()
        ),
    }


def process_kill(
    system, config, workdir, *, trials=20, seed=20261019, targets=None, emit=None
):
    generator = random.Random(seed)
    totals = Counter()
    results = []
    scope = Scope("benchmark", "process-kill")
    # Probe only opens storage. No measured writes happen before target selection.
    with Worker(system, config, workdir / "probe") as probe:
        info = probe.info
        targets = targets or (
            ("worker", *(f"server:{role}" for role in probe.servers), "both")
            if probe.servers
            else ("worker",)
        )
    for target in targets:
        for trial in range(trials):
            path = workdir / f"{target}-{trial}"
            worker = Worker(system, config, path)
            faulted = False
            writes = []
            try:
                for index in range(generator.randint(1, 8)):
                    record = Record.make(scope, f"key{index}", f"memory{index}")
                    ack = worker.call("write", scope=scope, record=record)
                    writes.append(
                        (
                            record,
                            ack.dropped,
                            worker.last_sent_ns,
                            worker.last_ack_ns,
                        )
                    )
                fault_ns = time.monotonic_ns()
                worker.kill(target)
                faulted = True
                expected = dict(worker.acked)
            finally:
                worker.close(preserve_server=faulted)
            for record, dropped, start_ns, ack_ns in writes:
                event(
                    emit,
                    operation="write",
                    trial=trial,
                    target=target,
                    record=asdict(record),
                    dropped=dropped,
                    start_ns=start_ns,
                    ack_ns=ack_ns,
                )
            event(
                emit, operation="fault", target=target, trial=trial, fault_ns=fault_ns
            )
            with Worker(system, config, path) as reader:
                rows = reader.call(
                    "list_prefix", scope=scope, prefix="", limit=len(expected) + 100
                )
                counts = inspect_records(expected, rows)
            totals.update(counts)
            detail = {
                "target": target,
                "trial": trial,
                "acked": len(expected),
                "fault_ns": fault_ns,
                **counts,
            }
            results.append(detail)
            event(emit, operation="recovery", **detail)
    return {
        "status": "violation" if any(totals.values()) else "ok",
        **dict(totals),
        "targets": {
            target: {
                metric: sum(r[metric] for r in results if r["target"] == target)
                for metric in ("acked_missing", "duplicates", "torn")
            }
            for target in targets
        },
        "trials": results,
        "note": PROCESS_NOTE,
        "info": info,
    }


def concurrent(
    suite,
    system,
    config,
    workdir,
    *,
    writer_counts=(2, 4, 8),
    writes=20,
    emit=None,
):
    scope = Scope("benchmark", suite)
    totals, trials = Counter(), []
    history_observations = []
    for count in writer_counts:
        path = workdir / f"writers-{count}"
        with ExitStack() as stack:
            workers = [
                stack.enter_context(Worker(system, config, path)) for _ in range(count)
            ]
            info, capabilities = workers[0].info, workers[0].capabilities
            method = "upsert" if suite == "concurrent-kv" else "write"
            if method not in capabilities:
                raise NotSupported(f"{method} is not supported")
            for phase in (
                ("distinct", "overlap") if suite == "concurrent-kv" else ("distinct",)
            ):
                barrier = threading.Barrier(count)
                expected = {}

                def writer(
                    index,
                    workers=workers,
                    barrier=barrier,
                    phase=phase,
                    method=method,
                    count=count,
                ):
                    worker, records = workers[index], []
                    barrier.wait(timeout=30)
                    for sequence in range(writes):
                        key = (
                            f"shared-{sequence % 4}"
                            if phase == "overlap"
                            else f"writer{index}-{sequence}"
                        )
                        record = Record.make(
                            scope, key, f"memory writer{index} value{sequence}"
                        )
                        args = {"scope": scope, "record": record}
                        ack = worker.call(method, **args)
                        if not ack.dropped:
                            records.append(record)
                        event(
                            emit,
                            operation=method,
                            phase=phase,
                            writer=index,
                            writers=count,
                            record=asdict(record),
                            dropped=ack.dropped,
                            start_ns=worker.last_sent_ns,
                            ack_ns=worker.last_ack_ns,
                        )
                    return records

                with ThreadPoolExecutor(max_workers=count) as pool:
                    for records in pool.map(writer, range(count)):
                        expected.update((r.id, r) for r in records)
                rows = workers[0].call(
                    "list_prefix",
                    scope=scope,
                    prefix="shared-" if phase == "overlap" else "writer",
                    limit=count * writes + 100,
                )
                if phase == "distinct":
                    counts = inspect_records(expected, rows)
                    counts["invalid_final"] = 0
                else:
                    keys = {r.key for r in expected.values()}
                    final = {
                        key: workers[0].call("get", scope=scope, key=key)
                        for key in keys
                    }
                    counts = inspect_overlap(expected, rows, final)
                    versions = Counter(
                        r.key
                        for r in {r.id: r for r in rows}.values()
                        if r.valid() and expected.get(r.id) == r
                    )
                    history_observations.append(
                        {
                            "writers": count,
                            "versions_per_key": dict(sorted(versions.items())),
                        }
                    )
                totals.update(counts)
                detail = {"writers": count, "phase": phase, **counts}
                trials.append(detail)
                event(emit, operation="concurrent-check", **detail)
    return {
        "status": "violation" if any(totals.values()) else "ok",
        **dict(totals),
        "trials": trials,
        "info": info,
        "cas": "not measured in B1",
        "history": {
            "advertised": "history" in capabilities,
            "max_versions_per_key": max(
                (
                    n
                    for observation in history_observations
                    for n in observation["versions_per_key"].values()
                ),
                default=0,
            ),
            "observations": history_observations,
        },
    }


def history_scale(
    system,
    config,
    workdir,
    *,
    sizes=(1000, 10000, 100000),
    repeats=20,
    emit=None,
):
    scope, curves, loaded = Scope("benchmark", "history-scale"), {}, 0
    with Worker(system, config, workdir) as worker:
        info = worker.info
        for size in sizes:
            while loaded < size:
                upper = min(loaded + 1000, size)
                records = [
                    Record.make(scope, f"key{i:08d}", f"memory needle{i:08d}")
                    for i in range(loaded, upper)
                ]
                worker.call("bulk_load", scope=scope, records=records)
                loaded = upper
            # Bulk validation is untimed. Each row carries the same indexed text.
            all_rows = worker.call(
                "list_prefix", scope=scope, prefix="", limit=size + 1
            )
            if (
                len(all_rows) != size
                or len({r.id for r in all_rows}) != size
                or {r.key for r in all_rows} != {f"key{i:08d}" for i in range(size)}
                or any(not r.valid() for r in all_rows)
            ):
                raise RuntimeError(f"Bulk load validation failed at {size}")
            known = next(r for r in all_rows if r.key == f"key{size // 2:08d}")
            ops = {}
            for op in ("read_recent", "get", "search"):
                if op not in worker.capabilities:
                    ops[op] = {"status": "N/A"}
                    continue
                latencies = []
                for repeat in range(repeats):
                    start = time.monotonic_ns()
                    if op == "read_recent":
                        result = worker.call(op, scope=scope, n=20)
                        valid = [r.key for r in result] == [
                            f"key{i:08d}"
                            for i in range(size - 1, max(-1, size - 21), -1)
                        ]
                    elif op == "get":
                        result = worker.call(op, scope=scope, key=known.key)
                        valid = result == known
                    else:
                        result = worker.call(op, scope=scope, query=known.text, k=20)
                        valid = known in result
                    elapsed = (time.monotonic_ns() - start) / 1e6
                    rows = result if isinstance(result, list) else [result]
                    if not valid or any(r and not r.valid() for r in rows):
                        raise RuntimeError(f"{op} returned invalid history at {size}")
                    latencies.append(elapsed)
                    event(
                        emit,
                        operation="history-read",
                        read_path=op,
                        size=size,
                        repeat=repeat,
                        latency_ms=elapsed,
                    )
                ops[op] = {
                    "latency_ms": percentiles(latencies),
                    "mapping": (
                        "emulated"
                        if f"emulated:{op}" in worker.capabilities
                        else "native"
                    ),
                }
            curves[str(size)] = ops
    return {
        "status": "ok",
        "sizes": curves,
        "info": info,
        "bulk_load_timed": False,
        "control_gate": dict(HISTORY_CONTROLS),
    }
