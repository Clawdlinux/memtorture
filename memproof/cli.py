import argparse


def main(argv=None):
    parser = argparse.ArgumentParser(description="memproof: evidence, not proof.")
    commands = parser.add_subparsers(dest="command", required=True)
    controls = commands.add_parser("controls", help="Check suite controls.")
    controls.add_argument("--quick", action="store_true", help="Run 5 trials.")
    args = parser.parse_args(argv)
    if args.command == "controls":
        from memproof.controls.gate import run_controls

        summary = run_controls(quick=args.quick)
        for suite, result in sorted(summary["suites"].items()):
            print(
                f"{suite}: fail={result['must_fail_rate']:.0%} "
                f"pass={result['must_pass_rate']:.0%} "
                f"discriminates={result['discriminates']}"
            )
        return int(not all(r["discriminates"] for r in summary["suites"].values()))
    return 0
