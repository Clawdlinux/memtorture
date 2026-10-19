import hashlib
import json
import platform
import shutil
import uuid
from pathlib import Path

from memtorture.results import ResultWriter, hardware
from memtorture.suites import (
    HISTORY_CONTROLS,
    PATHS,
    concurrent,
    process_kill,
    recall_delay,
)
from memtorture.types import NotSupported

DEFAULT_ROOT = Path("results/controls")


def methodology_hash():
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    for relative in (
        "docs/METHODOLOGY.md",
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
    return digest.hexdigest()


def control_gate(failures, passes, trials):
    return trials > 0 and failures / trials >= 0.9 and passes == trials


def require_controls(suite, root=DEFAULT_ROOT):
    if suite == "history-scale":
        return
    try:
        summary = json.loads((Path(root) / "gate.json").read_text())
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
        raise NotSupported(f"{suite}: controls missing, stale, or not discriminating")


def run_controls(*, quick=False, root=DEFAULT_ROOT):
    root = Path(root)
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
        ) as writer:
            for suite, fail_system, pass_system in (
                ("recall-delay", "delayed-index", "sqlite-visible"),
                ("process-kill", "buffered", "sqlite-off"),
                ("concurrent-append", "md-rmw-sleep", "sqlite-busy"),
                ("concurrent-kv", "md-rmw-sleep", "sqlite-busy"),
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
            summary["suites"]["history-scale"] = dict(HISTORY_CONTROLS)
            writer.manifest["control_summary"] = "gate.json"
        if not quick:
            (root / "gate.json").write_text(json.dumps(summary, indent=2) + "\n")
        (run_root / "gate.json").write_text(json.dumps(summary, indent=2) + "\n")
    finally:
        shutil.rmtree(work)
    return summary
