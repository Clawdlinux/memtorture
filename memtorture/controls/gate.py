import hashlib
import json
import platform
import shutil
import uuid
from dataclasses import asdict
from pathlib import Path

from memtorture.protocol import Worker
from memtorture.results import ResultWriter, hardware
from memtorture.suites import (
    HISTORY_CONTROLS,
    PATHS,
    concurrent,
    event,
    process_kill,
    recall_delay,
)
from memtorture.types import NotSupported, Record, Scope

DEFAULT_ROOT = Path("results/controls")
UNGATED_SUITES = ("history-scale", "write-throughput", "recovery")


def platform_key():
    return f"{platform.system().lower()}-{platform.machine().lower()}"


def methodology_hash():
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    for relative in (
        "docs/METHODOLOGY.md",
        "docs/PROTOCOL.md",
        "docs/design/0001-benchmark.md",
        "pyproject.toml",
        "uv.lock",
        "scripts/qdrant.sha256",
        "scripts/mcp/package-lock.json",
        "scripts/mcp/scan-verdict.json",
    ):
        path = root.parent / relative
        if path.exists():
            digest.update(relative.encode())
            digest.update(path.read_bytes())
    for directory in ("docs/protocol/v1", "systems"):
        for path in sorted((root.parent / directory).rglob("*")):
            if path.is_file() and path.suffix in {".json", ".jsonl", ".mjs"}:
                digest.update(str(path.relative_to(root.parent)).encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()


def control_gate(failures, passes, trials):
    return trials > 0 and failures / trials >= 0.9 and passes == trials


def require_controls(suite, root=DEFAULT_ROOT):
    if suite in UNGATED_SUITES:
        return
    path = Path(root) / platform_key() / "gate.json"
    try:
        summary = json.loads(path.read_text())
        valid = (
            summary["methodology_sha256"] == methodology_hash()
            and summary["quick"] is False
            and summary["trials"] >= 20
            and summary["os"] == platform.platform()
            and summary["hardware"] == hardware()
            and summary["suites"][suite]["discriminates"]
        )
    except (OSError, ValueError, KeyError):
        valid = False
    if not valid:
        raise NotSupported(
            f"{suite}: controls missing, stale, or not discriminating: {path}. "
            "Run memtorture controls on this platform."
        )


def check_supersede_by_key(worker, *, versions=5, emit=None):
    """Write versions through keyed upserts and check the final keyed read."""
    if versions < 2:
        raise ValueError("Supersession needs at least 2 versions")
    missing = {"keyed", "upsert", "get"} - worker.capabilities
    if missing:
        raise NotSupported(f"Missing {', '.join(sorted(missing))}")
    scope = Scope("benchmark", "supersede-by-key")
    dropped = 0
    for version in range(1, versions + 1):
        record = Record.make(scope, "k", f"v{version}")
        ack = worker.call("upsert", scope=scope, record=record)
        dropped += ack.dropped
        event(emit, operation="upsert", record=asdict(record), dropped=ack.dropped)
    observed = worker.call("get", scope=scope, key=record.key)
    result = {
        "status": (
            "ok"
            if not dropped and observed == record and observed.valid()
            else "violation"
        ),
        "versions": versions,
        "acked": versions - dropped,
        "dropped": dropped,
        "expected": asdict(record),
        "observed": asdict(observed) if observed is not None else None,
    }
    event(emit, operation="supersede-check", **result)
    return result


def run_controls(*, quick=False, root=DEFAULT_ROOT):
    root = Path(root) / platform_key()
    run_root = root / ("quick" if quick else "full")
    trials = 5 if quick else 20
    work = run_root / ".work" / uuid.uuid4().hex
    work.mkdir(parents=True)
    summary = {
        "trials": trials,
        "quick": quick,
        "os": platform.platform(),
        "hardware": hardware(),
        "methodology_sha256": methodology_hash(),
        "suites": {},
        "run_directory": str(run_root),
    }
    try:
        with ResultWriter(
            run_root,
            command=["memtorture", "controls"] + (["--quick"] if quick else []),
            control_evidence=True,
        ) as writer:
            for suite, fail_system, pass_system in (
                ("recall-delay", "delayed-index", "sqlite-visible"),
                ("process-kill", "buffered", "sqlite-off"),
                ("concurrent-append", "md-rmw-sleep", "sqlite-busy"),
                ("concurrent-kv", "md-rmw-sleep", "sqlite-busy"),
                ("supersede-by-key", "append-oldest", "sqlite-upsert"),
            ):
                counts, details = {"must_fail": 0, "must_pass": 0}, []
                for role, system in (
                    ("must_fail", fail_system),
                    ("must_pass", pass_system),
                ):
                    for trial in range(trials):
                        path = work / suite / role / str(trial)

                        def emit(
                            data, suite=suite, system=system, role=role, trial=trial
                        ):
                            writer.operation(
                                {
                                    "suite": suite,
                                    "system": system,
                                    "role": role,
                                    "control_trial": trial,
                                    **data,
                                }
                            )

                        if suite == "recall-delay":
                            read_path = list(PATHS)[trial % len(PATHS)]
                            result = recall_delay(
                                system,
                                "default",
                                path,
                                samples=1,
                                poll_ms=1,
                                paths=(read_path,),
                                emit=emit,
                            )
                            metric = result["paths"][read_path]
                            observation = metric["observations"][0]
                            lag = observation.get("lag_ms")
                            upper = 100 + 1 + metric["read_ms"]["p99"]
                            matched = (
                                lag is not None and 100 <= lag <= upper
                                if role == "must_fail"
                                else observation.get("polls") == 1
                            )
                            details.append(
                                {
                                    "role": role,
                                    "trial": trial,
                                    "path": read_path,
                                    "lag_ms": lag,
                                    "upper_ms": upper,
                                    "read_p99_ms": metric["read_ms"]["p99"],
                                    "matched": matched,
                                }
                            )
                        elif suite == "process-kill":
                            result = process_kill(
                                system,
                                "default",
                                path,
                                trials=1,
                                seed=20261019 + trial,
                                targets=("worker",),
                                emit=emit,
                            )
                            matched = result["status"] == (
                                "violation" if role == "must_fail" else "ok"
                            )
                            details.append({"role": role, "trial": trial, **result})
                        elif suite == "supersede-by-key":
                            with Worker(system, "default", path) as worker:
                                result = check_supersede_by_key(worker, emit=emit)
                                result["info"] = worker.info
                            matched = result["status"] == (
                                "violation" if role == "must_fail" else "ok"
                            )
                            details.append({"role": role, "trial": trial, **result})
                        else:
                            result = concurrent(
                                suite,
                                system,
                                "default",
                                path,
                                writer_counts=((2, 4, 8)[trial % 3],),
                                emit=emit,
                            )
                            matched = result["status"] == (
                                "violation" if role == "must_fail" else "ok"
                            )
                            details.append({"role": role, "trial": trial, **result})
                        counts[role] += bool(matched)
                        writer.cell(
                            {
                                "suite": suite,
                                "system": system,
                                "config": f"default;trial={trial}",
                                "matched": matched,
                                "result": result,
                            }
                        )
                summary["suites"][suite] = {
                    "must_fail": fail_system,
                    "must_pass": pass_system,
                    "must_fail_rate": counts["must_fail"] / trials,
                    "must_pass_rate": counts["must_pass"] / trials,
                    "discriminates": control_gate(
                        counts["must_fail"], counts["must_pass"], trials
                    ),
                    "details": details,
                    "gated": True,
                }
            summary["suites"].update(
                {suite: dict(HISTORY_CONTROLS) for suite in UNGATED_SUITES}
            )
            writer.manifest["control_summary"] = "gate.json"
        if not quick:
            (root / "gate.json").write_text(json.dumps(summary, indent=2) + "\n")
        (run_root / "gate.json").write_text(json.dumps(summary, indent=2) + "\n")
    finally:
        shutil.rmtree(work)
    return summary
