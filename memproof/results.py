import json
import os
import platform
import subprocess
import threading
from importlib.metadata import version
from pathlib import Path

SPONSOR = "Sponsored and built by the Clawdlinux team."


def commit():
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
    ).strip()


def hardware():
    ram = None
    if platform.system() == "Darwin":
        ram = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True))
    elif hasattr(os, "sysconf"):
        ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    return {
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "ram_bytes": ram,
    }


class ResultWriter:
    def __init__(self, root: Path, command: list[str]):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.raw = (self.root / "operations.jsonl").open("w")
        self.cells = []
        self.lock = threading.Lock()
        self.manifest = {
            "sponsor": SPONSOR,
            "command": command,
            "harness_commit": commit(),
            "harness_dirty": bool(
                subprocess.check_output(["git", "status", "--porcelain"], text=True)
            ),
            "os": platform.platform(),
            "python": platform.python_version(),
            "hardware": hardware(),
            "dependencies": {name: version(name) for name in ("mcp", "qdrant-client")},
            "systems": [],
            "comparative_publication": (
                "blocked pending outside review and reply window"
            ),
        }

    def operation(self, event):
        with self.lock:
            self.raw.write(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")
            self.raw.flush()

    def cell(self, cell):
        self.cells.append(cell)

    def close(self):
        self.raw.close()
        (self.root / "manifest.json").write_text(
            json.dumps(self.manifest, sort_keys=True, indent=2) + "\n"
        )
        (self.root / "cells.json").write_text(
            json.dumps(self.cells, sort_keys=True, indent=2) + "\n"
        )
        for suite in sorted({c["suite"] for c in self.cells}):
            lines = [
                SPONSOR,
                "",
                f"# {suite}",
                "",
                "Local evidence, not proof. Not cleared for comparative publication.",
                "",
                "| System | Config | Result |",
                "|---|---|---|",
            ]
            for cell in sorted(
                (c for c in self.cells if c["suite"] == suite),
                key=lambda c: (c["system"], c["config"]),
            ):
                detail = json.dumps(
                    {k: v for k, v in cell.items() if k not in {"system", "config"}},
                    sort_keys=True,
                ).replace("|", "\\|")
                lines.append(f"| {cell['system']} | {cell['config']} | {detail} |")
            (self.root / f"{suite}.md").write_text("\n".join(lines) + "\n")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
