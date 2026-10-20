import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from memtorture.controls.gate import (
    DEFAULT_ROOT,
    gate_path,
    methodology_hash,
    require_controls,
)
from memtorture.results import (
    LOCAL_BANNER,
    PUBLICATION_BLOCKED,
    RUNS_ROOT,
    ResultWriter,
    allowed_output,
    render_tables,
)
from memtorture.suites import (
    SUITES,
    concurrent,
    forget,
    forget_residue,
    history_scale,
    process_kill,
    recall_delay,
    recovery,
    supersede_by_key,
    write_throughput,
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
    if suite == "write-throughput":
        return write_throughput(
            system, config, path, writes=100 if extended else 20, emit=emit
        )
    if suite == "recovery":
        return recovery(
            system,
            config,
            path,
            sizes=(100000, 1000000) if extended else (100000,),
            emit=emit,
        )
    if suite in {"supersede-by-key", "forget", "forget-residue"}:
        body = {
            "supersede-by-key": supersede_by_key,
            "forget": forget,
            "forget-residue": forget_residue,
        }[suite]
        return body(system, config, path, emit=emit)
    if suite in {"concurrent-append", "concurrent-kv"}:
        return concurrent(suite, system, config, path, emit=emit)
    raise ValueError(f"Unknown suite: {suite}")


def run(args):
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    root = Path(args.output or RUNS_ROOT / f"{stamp}-{uuid.uuid4().hex[:8]}")
    allowed_output(root)
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
        controls_path = gate_path(DEFAULT_ROOT)
        writer.manifest["controls"] = (
            json.loads(controls_path.read_text()) if controls_path.exists() else None
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


EXPORT_FIELDS = (
    "command",
    "comparative_publication",
    "extended",
    "hardware",
    "harness_commit",
    "harness_dirty",
    "methodology_sha256",
    "os",
    "platform",
    "python",
    "runtime_dependencies",
    "sponsor",
)


def load_run(root=None):
    if root is None:
        manifests = sorted(RUNS_ROOT.glob("*/manifest.json"))
        if not manifests:
            raise ValueError("No local runs found")
        root = manifests[-1].parent
    root = Path(root)
    allowed_output(root)
    manifest = json.loads((root / "manifest.json").read_text())
    cells = json.loads((root / "cells.json").read_text())
    return root, manifest, cells


def report(root=None, *, local=False):
    root, manifest, cells = load_run(root)
    if PUBLICATION_BLOCKED and not local:
        raise NotSupported(
            "Governance blocks comparative tables. "
            "Use memtorture report --local to view them on this machine."
        )
    if manifest.get("methodology_sha256") != methodology_hash():
        raise NotSupported("Run uses an older methodology. Rerun every system.")
    for suite in {cell["suite"] for cell in cells}:
        require_controls(suite)
    tables = render_tables(cells)
    if local:
        print(LOCAL_BANNER)
        for table in tables.values():
            print()
            print(table, end="")
        print()
        print(LOCAL_BANNER)
        return 0
    for suite, table in tables.items():
        (root / f"{suite}.md").write_text(table)
    print(f"Local tables: {root}.")
    return 0


def export_system(system, root=None, out=None):
    root, manifest, cells = load_run(root)
    cells = [cell for cell in cells if cell["system"] == system]
    if not cells:
        raise ValueError(f"No {system} cells in {root}")
    out = Path(out or RUNS_ROOT / "exports" / f"{root.name}-{system}")
    allowed_output(out)
    out.mkdir(parents=True)
    exported = {key: manifest[key] for key in EXPORT_FIELDS if key in manifest}
    exported["exported_from"] = root.name
    exported["systems"] = [
        row for row in manifest.get("systems", []) if row["system"] == system
    ]
    (out / "manifest.json").write_text(
        json.dumps(exported, sort_keys=True, indent=2) + "\n"
    )
    (out / "cells.json").write_text(json.dumps(cells, sort_keys=True, indent=2) + "\n")
    with (
        (root / "operations.jsonl").open() as source,
        (out / "operations.jsonl").open("w") as target,
    ):
        for line in source:
            if json.loads(line).get("system") == system:
                target.write(line)
    print(f"Exported {system}: {out}")
    return 0
