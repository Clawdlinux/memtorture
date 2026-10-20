import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from memtorture.controls.gate import (
    DEFAULT_ROOT,
    methodology_hash,
    platform_key,
    require_controls,
)
from memtorture.results import ResultWriter, render_tables
from memtorture.suites import (
    SUITES,
    concurrent,
    history_scale,
    process_kill,
    recall_delay,
)
from memtorture.types import NotSupported

ROWS = {
    "markdown": ("append", "rewrite"),
    "mcp-memory": ("default",),
    "qdrant": ("wait=false", "wait=true"),
    "sqlite": ("synchronous=NORMAL", "synchronous=FULL"),
}


def run_cell(suite, system, config, path, *, extended=False, emit=None):
    require_controls(suite)
    if suite == "recall-delay":
        return recall_delay(system, config, path, emit=emit)
    if suite == "history-scale":
        sizes = (1000, 10000, 100000) + ((1000000,) if extended else ())
        return history_scale(system, config, path, sizes=sizes, emit=emit)
    if suite == "process-kill":
        return process_kill(system, config, path, emit=emit)
    return concurrent(suite, system, config, path, emit=emit)


def run(args):
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    root = args.output or Path("results/runs") / f"{stamp}-{uuid.uuid4().hex[:8]}"
    root = Path(root)
    controls = Path("results/controls").resolve()
    if root.resolve() == controls or controls in root.resolve().parents:
        raise ValueError("Comparative cells cannot be written under results/controls")
    if (root / "manifest.json").exists():
        raise ValueError("Output already has a manifest. Choose a new directory.")
    selected = (
        [
            (suite, system, row)
            for suite in SUITES
            for system in sorted(ROWS)
            for row in ROWS[system]
        ]
        if args.all
        else [(args.suite, args.system, args.config)]
    )
    failures = 0
    with ResultWriter(root, args.invocation) as writer:
        writer.manifest["methodology_sha256"] = methodology_hash()
        writer.manifest["extended"] = args.extended
        writer.manifest["blocked_suites"] = []
        gate_path = DEFAULT_ROOT / platform_key() / "gate.json"
        writer.manifest["controls"] = (
            json.loads(gate_path.read_text()) if gate_path.exists() else None
        )
        for suite, system, config in selected:
            try:
                require_controls(suite)
            except NotSupported as exc:
                writer.operation(
                    {
                        "operation": "cell-blocked",
                        "suite": suite,
                        "system": system,
                        "config": config,
                        "reason": str(exc),
                    }
                )
                if suite not in writer.manifest["blocked_suites"]:
                    writer.manifest["blocked_suites"].append(suite)
                failures += 1
                print(f"{suite} {system} {config}: blocked ({exc})", flush=True)
                continue

            def emit(data, suite=suite, system=system, config=config):
                writer.operation(
                    {
                        "suite": suite,
                        "system": system,
                        "config": config,
                        **data,
                    }
                )

            path = root / ".work" / suite / system / config.replace("=", "-")
            try:
                result = run_cell(
                    suite, system, config, path, extended=args.extended, emit=emit
                )
            except NotSupported as exc:
                result = {"status": "N/A", "reason": str(exc)}
            except (RuntimeError, OSError, ValueError, TimeoutError) as exc:
                result = {"status": "error", "reason": str(exc)}
                failures += 1
            writer.cell(
                {
                    "suite": suite,
                    "system": system,
                    "config": config,
                    **result,
                }
            )
            print(f"{suite} {system} {config}: {result['status']}", flush=True)
    print(f"Local output: {root}")
    return int(bool(failures))


def report(root=None):
    if root is None:
        manifests = sorted(Path("results/runs").glob("*/manifest.json"))
        if not manifests:
            raise ValueError("No local runs found")
        root = manifests[-1].parent
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("methodology_sha256") != methodology_hash():
        raise NotSupported("Run uses an older methodology. Rerun every system.")
    cells = json.loads((root / "cells.json").read_text())
    for suite in {cell["suite"] for cell in cells}:
        require_controls(suite)
    render_tables(root, cells)
    print(f"Local tables: {root}. Comparative publication remains blocked.")
    return 0
