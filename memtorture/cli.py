import argparse
import sys
from pathlib import Path

from memtorture.types import NotSupported


def main(argv=None):
    from memtorture.runner import ROWS, export_system, report, run
    from memtorture.suites import PROCESS_NOTE, SUITES

    parser = argparse.ArgumentParser(description="memtorture: evidence, not proof.")
    commands = parser.add_subparsers(dest="command", required=True)
    controls = commands.add_parser("controls", help="Check suite controls.")
    controls.add_argument("--quick", action="store_true", help="Run 5 trials.")
    conformance = commands.add_parser(
        "conformance", help="Replay protocol v1 fixtures."
    )
    conformance.add_argument("--command", dest="worker_command", required=True)
    conformance.add_argument("--config", default="default")
    run_parser = commands.add_parser(
        "run", help="Run local cells.", description=PROCESS_NOTE
    )
    run_parser.add_argument("--suite", choices=SUITES)
    run_parser.add_argument("--system", choices=sorted(ROWS))
    run_parser.add_argument(
        "--config", help="Exact setting. Never a durability nickname."
    )
    run_parser.add_argument(
        "--all",
        action="store_true",
        help="Run every suite and config row at default sizes.",
    )
    run_parser.add_argument(
        "--extended",
        action="store_true",
        help="Grow history and recovery to 1M; use 100 writes per writer.",
    )
    run_parser.add_argument("--output", type=Path, help="New local output directory.")
    report_parser = commands.add_parser(
        "report", help="Show comparative tables on this machine."
    )
    report_parser.add_argument(
        "--input", type=Path, help="Run directory. Defaults to latest."
    )
    report_parser.add_argument(
        "--local",
        action="store_true",
        help="Print tables to stdout for the owner only. Writes no files.",
    )
    export_parser = commands.add_parser(
        "export-system", help="Write one system's raw output for its upstream issue."
    )
    export_parser.add_argument("--system", choices=sorted(ROWS), required=True)
    export_parser.add_argument(
        "--input", type=Path, help="Run directory. Defaults to latest."
    )
    export_parser.add_argument(
        "--out", type=Path, help="New directory under results/runs/exports/."
    )
    args = parser.parse_args(argv)
    args.invocation = ["memtorture", *(sys.argv[1:] if argv is None else argv)]
    if args.command == "conformance":
        from memtorture.conformance import run_conformance

        try:
            result = run_conformance(args.worker_command, config=args.config)
        except (OSError, ValueError, RuntimeError, TimeoutError, NotSupported) as exc:
            print(f"FAIL: {exc}")
            return 1
        print(
            f"PASS: {result['passed']} worker transcript checks; "
            f"{len(result['skipped'])} unsupported checks"
        )
        print(f"HARNESS: {result['harness_checks']} decoder self-checks")
        for reason in result["skipped"]:
            print(f"SKIP: {reason}")
        return 0
    if args.command == "run":
        filters = (args.suite, args.system, args.config)
        if args.all and any(filters):
            parser.error("--all cannot be combined with cell filters")
        if not args.all and not all(filters):
            parser.error("Give --suite, --system, --config, or use --all")
        if not args.all and args.config not in ROWS[args.system]:
            parser.error(f"Rows for {args.system}: {', '.join(ROWS[args.system])}")
        try:
            return run(args)
        except (OSError, ValueError, NotSupported) as exc:
            parser.exit(1, f"{exc}\n")
    if args.command in {"report", "export-system"}:
        try:
            if args.command == "report":
                return report(args.input, local=args.local)
            return export_system(args.system, args.input, args.out)
        except (OSError, ValueError, NotSupported) as exc:
            print(exc)
            return 1
    if args.command == "controls":
        from memtorture.controls.gate import run_controls

        summary = run_controls(quick=args.quick)
        for suite, result in sorted(summary["suites"].items()):
            if result.get("gated") is False:
                print(f"{suite}: ungated ({result['controls']})")
                continue
            print(
                f"{suite}: fail={result['must_fail_rate']:.0%} "
                f"pass={result['must_pass_rate']:.0%} "
                f"discriminates={result['discriminates']}"
            )
        return int(
            not all(
                r["discriminates"]
                for r in summary["suites"].values()
                if r.get("gated", True)
            )
        )
    return 0
