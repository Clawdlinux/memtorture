import json
import os
import platform
import subprocess
import threading
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
    def __init__(self, root: Path, command: list[str], *, control_evidence=False):
        from memtorture.controls.gate import methodology_hash

        status_command = ["git", "status", "--porcelain"]
        if control_evidence:
            status_command += ["--", ":/", ":(top,exclude)results/controls"]
        provenance = {
            "harness_commit": commit(),
            "harness_dirty": bool(subprocess.check_output(status_command, text=True)),
        }
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.raw = (self.root / "operations.jsonl").open("w")
        self.cells = []
        self.lock = threading.Lock()
        self.manifest = {
            "sponsor": SPONSOR,
            "command": command,
            **provenance,
            "methodology_sha256": methodology_hash(),
            "os": platform.platform(),
            "python": platform.python_version(),
            "hardware": hardware(),
            "runtime_dependencies": "none (Python standard library only)",
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
        info = cell.get("info", cell.get("result", {}).get("info", {}))
        system = {
            "system": cell["system"],
            "config": cell["config"].split(";trial=")[0],
            **info,
        }
        if system not in self.manifest["systems"]:
            self.manifest["systems"].append(system)
            self.manifest["systems"].sort(
                key=lambda row: (row["system"], row["config"])
            )

    def close(self):
        self.raw.close()
        (self.root / "manifest.json").write_text(
            json.dumps(self.manifest, sort_keys=True, indent=2) + "\n"
        )
        (self.root / "cells.json").write_text(
            json.dumps(self.cells, sort_keys=True, indent=2) + "\n"
        )
        render_tables(self.root, self.cells)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def render_tables(root, cells):
    for suite in sorted({c["suite"] for c in cells}):
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
            (c for c in cells if c["suite"] == suite),
            key=lambda c: (c["system"], c["config"]),
        ):
            detail = json.dumps(
                {k: v for k, v in cell.items() if k not in {"system", "config"}},
                sort_keys=True,
            ).replace("|", "\\|")
            lines.append(f"| {cell['system']} | {cell['config']} | {detail} |")
        (Path(root) / f"{suite}.md").write_text("\n".join(lines) + "\n")
