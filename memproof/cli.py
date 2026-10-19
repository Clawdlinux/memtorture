import argparse
import sys
from pathlib import Path

from memproof.types import NotSupported


def main(argv=None):
    from memproof.runner import ROWS, report, run
    from memproof.suites import PROCESS_NOTE, SUITES

    parser = argparse.ArgumentParser(description="memproof: evidence, not proof.")
    commands = parser.add_subparsers(dest="command", required=True)
    controls = commands.add_parser("controls", help="Check suite controls.")
    controls.add_argument("--quick", action="store_true", help="Run 5 trials.")
    run_parser = commands.add_parser(
        "run", help="Run local cells.", description=PROCESS_NOTE
    )
    run_parser.add_argument("--suite", choices=SUITES)
    run_parser.add_argument("--system", choices=sorted(ROWS))
    run_parser.add_argument(
        "--config", help="Exact setting. Never a durability nickname."
    )
    run_parser.add_argument("--all", action="store_true", help="Run all 35 B1 cells.")
    run_parser.add_argument(
        "--extended", action="store_true", help="Grow history to 1M."
    )
    run_parser.add_argument("--output", type=Path, help="New local output directory.")
    report_parser = commands.add_parser("report", help="Rebuild local tables.")
    report_parser.add_argument(
        "--input", type=Path, help="Run directory. Defaults to latest."
    )
    args = parser.parse_args(argv)
    args.invocation = ["memproof", *(sys.argv[1:] if argv is None else argv)]
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
    if args.command == "report":
        try:
            return report(args.input)
        except (OSError, ValueError, NotSupported) as exc:
            print(exc)
            return 1
    if args.command == "controls":
        from memproof.controls.gate import run_controls

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
