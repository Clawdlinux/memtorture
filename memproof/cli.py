import argparse


def main(argv=None):
    parser = argparse.ArgumentParser(description="memproof: evidence, not proof.")
    commands = parser.add_subparsers(dest="command", required=True)
    controls = commands.add_parser("controls", help="Check suite controls.")
    controls.add_argument("--quick", action="store_true", help="Run 5 trials.")
    args = parser.parse_args(argv)
    if args.command == "controls":
        print("Control gate not implemented. Scaffold only. No cells can publish.")
    return 0
