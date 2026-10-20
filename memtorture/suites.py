import json
import math
import platform
import random
import secrets
import subprocess
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path

from memtorture.protocol import Worker
from memtorture.types import NotSupported, Record, Scope

SUITES = (
    "recall-delay",
    "history-scale",
    "process-kill",
    "concurrent-append",
    "concurrent-kv",
    "write-throughput",
    "supersede-by-key",
    "forget",
    "forget-residue",
    "recovery",
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
    count = len(values)
    return {
        "n": count,
        "p50": values[math.ceil(count * 0.5) - 1] if count else None,
        "p99": values[math.ceil(count * 0.99) - 1] if count >= 100 else None,
    }


def event(emit, **data):
    if emit:
        emit({"monotonic_ns": time.monotonic_ns(), **data})


def check_supersede_by_key(worker, *, versions=5, emit=None):
    """Check the latest keyed value against interleaved decoy writes."""
    if versions < 2:
        raise ValueError("Supersession needs at least 2 versions")
    missing = {"keyed", "upsert", "get"} - worker.capabilities
    if missing:
        raise NotSupported(f"Missing {', '.join(sorted(missing))}")
    scope = Scope("benchmark", "supersede-by-key")
    dropped = 0
    for version in range(1, versions + 1):
        record = Record.make(scope, "k", f"v{version}")
        decoy = Record.make(scope, "decoy", f"decoy{version}")
        for written in (record, decoy):
            ack = worker.call("upsert", scope=scope, record=written)
            dropped += ack.dropped
            event(emit, operation="upsert", record=asdict(written), dropped=ack.dropped)
    observed = worker.call("get", scope=scope, key=record.key)
    result = {
        "status": (
            "ok"
            if not dropped and observed == record and observed.valid()
            else "violation"
        ),
        "versions": versions,
        "acked": 2 * versions - dropped,
        "dropped": dropped,
        "expected": asdict(record),
        "observed": asdict(observed) if observed is not None else None,
    }
    event(emit, operation="supersede-check", **result)
    return result


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
                "read_max_ms": max(read_latencies, default=None),
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


def intact(record, expected):
    return record.valid() and (
        record.id not in expected or record == expected[record.id]
    )


def inspect_records(expected, rows, *, keyed=False):
    by_id = {r.id: r for r in rows}
    valid = [r for r in rows if intact(r, expected)]
    counts = Counter(r.id for r in valid)
    return {
        "acked_missing": sum(r.id not in by_id for r in expected.values()),
        "duplicates": sum(max(0, count - 1) for count in counts.values()),
        "torn": len(rows) - len(valid),
    }


def inspect_overlap(expected, rows, final):
    valid = [r for r in rows if intact(r, expected)]
    counts = Counter(r.id for r in valid)
    return {
        "acked_missing": 0,
        "duplicates": sum(max(0, count - 1) for count in counts.values()),
        "torn": len(rows) - len(valid),
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
                    valid_by_id = {}
                    for record in rows:
                        if record.id in expected and intact(record, expected):
                            valid_by_id.setdefault(record.id, record)
                    versions = Counter(r.key for r in valid_by_id.values())
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


def supersede_by_key(system, config, workdir, *, versions=5, emit=None):
    with Worker(system, config, workdir) as worker:
        return {
            **check_supersede_by_key(worker, versions=versions, emit=emit),
            "info": worker.info,
        }


def poll_until(fn, *, timeout_s, poll_ms):
    if timeout_s < 0 or poll_ms < 0:
        raise ValueError("Polling durations must be nonnegative")
    deadline = time.monotonic() + timeout_s
    while True:
        if fn():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(poll_ms / 1000, remaining))


def write_throughput(
    system,
    config,
    workdir,
    *,
    writer_counts=(1, 8, 64),
    writes=20,
    seed=20261020,
    timeout_s=5,
    emit=None,
):
    if writes < 1 or not writer_counts or any(n < 1 for n in writer_counts):
        raise ValueError("Positive writer counts and writes are required")
    scope, trials = Scope("benchmark", "write-throughput"), []
    for count in writer_counts:
        with ExitStack() as stack:
            workers = [
                stack.enter_context(
                    Worker(system, config, workdir / f"writers-{count}")
                )
                for _ in range(count)
            ]
            info = workers[0].info
            if not {"write", "list_prefix"} <= workers[0].capabilities:
                raise NotSupported("Throughput needs write and list_prefix readback")
            generator = random.Random(seed)
            batches = [
                [
                    Record.make(
                        scope,
                        f"writer{writer}-{i}",
                        "x"
                        * generator.choices((128, 1024, 8192), weights=(6, 3, 1), k=1)[
                            0
                        ],
                    )
                    for i in range(writes)
                ]
                for writer in range(count)
            ]
            released = []
            barrier = threading.Barrier(
                count,
                action=lambda released=released: released.append(time.monotonic_ns()),
            )

            def write_batch(
                index, workers=workers, batches=batches, barrier=barrier, count=count
            ):
                worker, observations = workers[index], []
                barrier.wait(timeout=30)
                for record in batches[index]:
                    ack = worker.call("write", scope=scope, record=record)
                    observations.append(
                        {
                            "operation": "write",
                            "writer": index,
                            "writers": count,
                            "record_id": record.id,
                            "text_bytes": len(record.text.encode()),
                            "dropped": ack.dropped,
                            "start_ns": worker.last_sent_ns,
                            "ack_ns": worker.last_ack_ns,
                        }
                    )
                return observations

            with ThreadPoolExecutor(max_workers=count) as pool:
                observations = [
                    entry
                    for batch in pool.map(write_batch, range(count))
                    for entry in batch
                ]
            end_ns = max(entry["ack_ns"] for entry in observations)
            expected = {
                key: record
                for worker in workers
                for key, record in worker.acked.items()
            }
            counts = {}

            def readback(worker=workers[0], count=count, expected=expected):
                nonlocal counts
                rows = worker.call(
                    "list_prefix",
                    scope=scope,
                    prefix="writer",
                    limit=count * writes + 1,
                )
                counts = inspect_records(expected, rows)
                return not counts["acked_missing"]

            poll_until(readback, timeout_s=timeout_s, poll_ms=10)
            detail = {
                "writers": count,
                "attempted": count * writes,
                "acked": len(expected),
                "dropped": sum(entry["dropped"] for entry in observations),
                "start_ns": released[0],
                "end_ns": end_ns,
                "window_ms": (end_ns - released[0]) / 1e6,
                "ops_per_second": len(expected) * 1e9 / (end_ns - released[0]),
                "write_latency_ms": percentiles(
                    [
                        (entry["ack_ns"] - entry["start_ns"]) / 1e6
                        for entry in observations
                    ]
                ),
                **counts,
            }
            trials.append(detail)
            for index, worker in enumerate(workers):
                event(
                    emit,
                    operation="worker-start",
                    writer=index,
                    writers=count,
                    spawn_ns=worker.spawn_ns,
                    start_reply_ns=worker.start_reply_ns,
                )
            for entry in observations:
                event(emit, **entry)
            event(emit, operation="throughput-check", **detail)
    return {
        "status": "ok",
        "trials": trials,
        "acked_missing": sum(trial["acked_missing"] for trial in trials),
        "seed": seed,
        "info": info,
        "readback_timeout_s": timeout_s,
        "control_gate": dict(HISTORY_CONTROLS),
    }


FORGET_PATHS = ("get", "get_by_ref", "list_prefix", "read_recent", "search")


def forget_promise(system):
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "systems" / system / "manifest.json").read_text())
    promise = manifest.get("forget_promise", {})
    if promise.get("kind") not in {"delete", "erase"} or not all(
        isinstance(promise.get(field), str) and promise[field]
        for field in ("url", "quote")
    ):
        raise NotSupported("No documented forget promise in the system manifest")
    return promise


def forget_read(worker, path, record):
    if path == "get":
        row = worker.call(path, scope=record.scope, key=record.key)
        return [row] if row else []
    return read_path(
        worker, next(name for name, op in PATHS.items() if op == path), record
    )


def seed_forget(worker, text):
    if not {"write", "delete"} <= worker.capabilities:
        raise NotSupported("Forget needs write and delete")
    scope = Scope("benchmark", "forget")
    for index in range(3):
        decoy = Record.make(scope, f"decoy-{index}", f"survivor {index}")
        if worker.call("write", scope=scope, record=decoy).dropped:
            raise RuntimeError("Forget setup write dropped")
    target = Record.make(scope, "target", text)
    ack = worker.call("write", scope=scope, record=target)
    if ack.dropped:
        raise RuntimeError("Forget target write dropped")
    if ack.ref is None:
        raise NotSupported("Write returned no native ref for delete")
    return target, ack.ref


def check_forget(worker, target, ref, *, timeout_s=5, poll_ms=10, emit=None):
    paths = {}
    for path in FORGET_PATHS:
        if path not in worker.capabilities:
            paths[path] = {"status": "N/A", "reason": f"Missing {path}"}
            continue

        def visible(path=path):
            return any(
                row == target and row.valid()
                for row in forget_read(worker, path, target)
            )

        try:
            if poll_until(visible, timeout_s=timeout_s, poll_ms=poll_ms):
                paths[path] = {"visible_before": True}
            else:
                paths[path] = {
                    "status": "not measurable",
                    "visible_before": False,
                    "reason": "Target was not visible before delete",
                }
        except NotSupported as exc:
            paths[path] = {"status": "N/A", "reason": str(exc)}
    worker.call("delete", scope=target.scope, ref=ref)
    ack_ns = time.monotonic_ns()
    pending = {path for path, detail in paths.items() if detail.get("visible_before")}

    def absent():
        for path in FORGET_PATHS:
            if path not in pending:
                continue
            rows = forget_read(worker, path, target)
            now = time.monotonic_ns()
            remaining = sum(
                row.id == target.id or row.key == target.key for row in rows
            )
            paths[path].update(remaining=remaining)
            if not remaining:
                paths[path].update(status="ok", lag_ms=(now - ack_ns) / 1e6)
                pending.remove(path)
        return not pending

    poll_until(absent, timeout_s=timeout_s, poll_ms=poll_ms)
    for path in pending:
        paths[path].update(status="violation", lag_ms=None)
    for path, detail in paths.items():
        event(emit, operation="forget-check", path=path, **detail)
    statuses = [detail["status"] for detail in paths.values()]
    status = "ok"
    if "violation" in statuses:
        status = "violation"
    elif "not measurable" in statuses:
        status = "not measurable"
    elif all(value == "N/A" for value in statuses):
        status = "N/A"
    return {"status": status, "paths": paths, "timeout_s": timeout_s}


def scan_plaintext(directories, token):
    needle, seen, occurrences = token.encode(), set(), 0
    for directory in directories:
        root = Path(directory)
        if not root.exists():
            raise RuntimeError("Reported data directory does not exist")
        for path in [root] if root.is_file() else root.rglob("*"):
            if not path.is_file() or path.resolve() in seen:
                continue
            seen.add(path.resolve())
            with path.open("rb") as source:
                tail = b""
                while chunk := source.read(1024 * 1024):
                    data = tail + chunk
                    occurrences += data.count(needle)
                    tail = data[-(len(needle) - 1) :]
    return occurrences


def forget(system, config, workdir, *, timeout_s=5, poll_ms=10, emit=None):
    promise, token = forget_promise(system), secrets.token_hex(16)
    with Worker(system, config, workdir) as worker:
        info = worker.info
        target, ref = seed_forget(worker, f"forget marker {token}")
        directories = worker.call("data_dirs") if promise["kind"] == "erase" else []
        before = scan_plaintext(directories, token) if directories else 0
        result = check_forget(
            worker, target, ref, timeout_s=timeout_s, poll_ms=poll_ms, emit=emit
        )
    if promise["kind"] == "erase":
        after = scan_plaintext(directories, token)
        result["occurrences_before"], result["occurrences_after"] = before, after
        if result["status"] == "ok":
            result["status"] = (
                "not measurable" if not before else "violation" if after else "ok"
            )
    return {**result, "forget_promise": promise, "info": info}


def forget_residue(system, config, workdir, *, timeout_s=5, poll_ms=10, emit=None):
    token = secrets.token_hex(16)
    with Worker(system, config, workdir) as worker:
        info = worker.info
        target, ref = seed_forget(worker, f"plaintext marker {token}")
        directories = worker.call("data_dirs")
        before = scan_plaintext(directories, token)
        if not before:
            result = {
                "status": "not measurable",
                "occurrences_before": 0,
                "reason": (
                    "Plaintext was not visible before delete; "
                    "compressed or encoded storage"
                ),
            }
        else:
            deletion = check_forget(
                worker, target, ref, timeout_s=timeout_s, poll_ms=poll_ms, emit=emit
            )
            result = {"occurrences_before": before, "forget": deletion}
    if before:
        after = scan_plaintext(directories, token)
        result.update(
            status="violation"
            if after or deletion["status"] == "violation"
            else deletion["status"],
            occurrences_after=after,
            bytes_found=after * len(token),
            scan_after_stop=True,
        )
    event(emit, operation="residue-check", **result)
    return {**result, "info": info}


def rss_bytes(pid):
    if platform.system() == "Linux":
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
        raise RuntimeError("Process has no VmRSS")
    if platform.system() == "Darwin":
        try:
            output = subprocess.check_output(
                ["ps", "-o", "rss=", "-p", str(pid)], text=True
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"RSS unavailable for process {pid}") from exc
        return int(output.strip()) * 1024
    raise NotSupported("RSS measurement requires Linux or macOS")


def recovery_record(scope, index):
    return Record.make(
        scope, f"key{index:08d}", f"memory needle{index:08d}", id=f"recovery-{index}"
    )


def recovery(system, config, workdir, *, sizes=(100000,), samples=100, emit=None):
    if samples < 1 or not sizes or any(size < 1 for size in sizes):
        raise ValueError("Positive sizes and samples are required")
    scope, curves = Scope("benchmark", "recovery"), {}
    for size in sizes:
        path = workdir / f"records-{size}"
        with Worker(system, config, path) as loader:
            if "get" not in loader.capabilities:
                raise NotSupported("Recovery needs get for known-record readiness")
            for lower in range(0, size, 1000):
                loader.call(
                    "bulk_load",
                    scope=scope,
                    records=[
                        recovery_record(scope, i)
                        for i in range(lower, min(lower + 1000, size))
                    ],
                )
        known = recovery_record(scope, size // 2)
        with Worker(system, config, path) as worker:
            info = worker.info

            def ready(worker=worker, known=known):
                return worker.call("get", scope=scope, key=known.key) == known

            if not poll_until(ready, timeout_s=60, poll_ms=10):
                raise RuntimeError("Known record unavailable after restart")
            ready_ns = time.monotonic_ns()
            start_to_read_ms = (ready_ns - worker.start_reply_ns) / 1e6
            open_ms = info.get("open_ms")
            pids = worker.servers or {"worker": worker.process.pid}
            memory = {role: rss_bytes(pid) for role, pid in pids.items()}
            indices = sorted(
                {
                    i * (size - 1) // max(1, min(size, samples) - 1)
                    for i in range(min(size, samples))
                }
            )
            missing = 0
            for index in indices:
                expected = recovery_record(scope, index)
                observed = worker.call("get", scope=scope, key=expected.key)
                missing += observed != expected or not observed.valid()
            verification = {
                "samples_checked": len(indices),
                "samples_missing": missing,
                "count": None,
                "count_matches": None,
                "count_source": "N/A",
                "enumeration_pages": 0,
            }
            if "list_prefix" in worker.capabilities:
                count, seen, intact = 0, set(), True
                for page in range((size + 9999) // 10000):
                    prefix = f"key{page:04d}"
                    rows = worker.call(
                        "list_prefix", scope=scope, prefix=prefix, limit=10001
                    )
                    count += len(rows)
                    seen.update(row.id for row in rows)
                    intact = intact and all(
                        row.valid() and row.key.startswith(prefix) for row in rows
                    )
                    verification["enumeration_pages"] += 1
                verification.update(
                    count=count,
                    count_source="list_prefix sub-prefix enumeration",
                    count_matches=count == size and len(seen) == size and intact,
                )
            curves[str(size)] = {
                "time_to_ready_ms": (ready_ns - worker.spawn_ns) / 1e6,
                "spawn_to_start_ms": (worker.start_reply_ns - worker.spawn_ns) / 1e6,
                "open_ms": open_ms,
                "start_to_first_read_ms": start_to_read_ms,
                "recovery_ms": open_ms + start_to_read_ms
                if open_ms is not None
                else None,
                "rss_bytes": memory,
                "verification": verification,
            }
            event(emit, operation="recovery-check", size=size, **curves[str(size)])
    return {
        "status": "violation"
        if any(
            curve["verification"]["samples_missing"]
            or curve["verification"]["count_matches"] is False
            for curve in curves.values()
        )
        else "ok",
        "sizes": curves,
        "info": info,
        "bulk_load_timed": False,
        "control_gate": dict(HISTORY_CONTROLS),
    }
