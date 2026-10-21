import hashlib
import json
import platform
import shutil
from pathlib import Path

from memtorture.protocol import Worker
from memtorture.results import ResultWriter, allowed_output, hardware, open_output
from memtorture.suites import (
    HISTORY_CONTROLS,
    PATHS,
    check_supersede_by_key,
    concurrent,
    forget,
    forget_residue,
    process_kill,
    recall_delay,
)
from memtorture.types import NotSupported

DEFAULT_ROOT = Path("results/controls")
UNGATED_SUITES = ("history-scale", "write-throughput", "recovery")


def platform_key():
    machine = platform.machine().lower()
    arch = {"aarch64": "arm64", "amd64": "x86_64"}.get(machine, machine)
    return f"{platform.system().lower()}-{arch}"


def gate_path(root=DEFAULT_ROOT):
    return Path(root) / platform_key() / "gate.json"


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
    path = gate_path(root)
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


def run_controls(*, quick=False):
    run_root = gate_path().parent / ("quick" if quick else "full")
    trials = 5 if quick else 20
    scratch = allowed_output(run_root / ".work", control_evidence=True)
    if scratch.exists():
        # A crashed run left scratch. The policy above refused a symlink.
        shutil.rmtree(scratch)
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
            work = writer.work
            work.mkdir(exist_ok=False)
            for suite, fail_system, pass_system in (
                ("recall-delay", "delayed-index", "sqlite-visible"),
                ("process-kill", "buffered", "sqlite-off"),
                ("concurrent-append", "md-rmw-sleep", "sqlite-busy"),
                ("concurrent-kv", "md-rmw-sleep", "sqlite-busy"),
                ("supersede-by-key", "append-oldest", "sqlite-upsert"),
                ("forget", "sqlite-forget-fts", "sqlite-forget-both"),
                ("forget-residue", "sqlite-residue-off", "sqlite-residue-secure"),
            ):
                counts, details = {"must_fail": 0, "must_pass": 0}, []
                for role, system in (
                    ("must_fail", fail_system),
                    ("must_pass", pass_system),
                ):
                    for trial in range(trials):
                        path = allowed_output(
                            work / suite / role / str(trial), control_evidence=True
                        )
                        path.mkdir(parents=True, exist_ok=False)

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
                            read_bound = metric["read_ms"]["p99"]
                            statistic = "p99"
                            if read_bound is None:
                                read_bound = metric["read_max_ms"]
                                statistic = "max"
                            upper = 100 + 1 + read_bound
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
                                    "read_ms": metric["read_ms"],
                                    "upper_read_ms": read_bound,
                                    "upper_read_statistic": statistic,
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
                        elif suite in {"forget", "forget-residue"}:
                            body = forget if suite == "forget" else forget_residue
                            result = body(
                                system,
                                "default",
                                path,
                                timeout_s=0.1,
                                poll_ms=1,
                                emit=emit,
                            )
                            matched = result["status"] == (
                                "violation" if role == "must_fail" else "ok"
                            )
                            details.append({"role": role, "trial": trial, **result})
                        elif suite == "supersede-by-key":
                            with Worker(system, "default", path) as worker:
                                result = check_supersede_by_key(
                                    worker, timeout_s=0.1, poll_ms=1, emit=emit
                                )
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
        for path in ([gate_path()] if not quick else []) + [run_root / "gate.json"]:
            with open_output(path, control_evidence=True) as output:
                output.write(json.dumps(summary, indent=2) + "\n")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return summary
