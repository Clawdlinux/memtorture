import json
import os
import platform
import subprocess
import threading
from pathlib import Path

SPONSOR = "Sponsored and built by the Clawdlinux team."
RUNS_ROOT = Path("results/runs")
LOCAL_BANNER = "LOCAL ONLY. Not for publication. Governance blocks comparative results."

PUBLICATION_BLOCKED = True
"""Governance blocks comparative results across systems.

Publication needs an outside maintainer to approve the methodology and a 14-day
reply window for each upstream project. See the Governance section of
docs/design/0001-benchmark.md. While this is True, run output stays raw and
multi-system tables only reach stdout through `memtorture report --local`.
"""


def allowed_output(path, *, control_evidence=False, under=None):
    """Return the output path, or reject it outside the allowed tree.

    Runs go under results/runs/. Control evidence goes under
    results/controls/<os>-<arch>/. The check is lexical from the working
    directory, and no existing part below results/ may be a symlink.
    """
    from memtorture.controls.gate import DEFAULT_ROOT, platform_key

    allowed = under or (
        DEFAULT_ROOT / platform_key() if control_evidence else RUNS_ROOT
    )
    base = Path.cwd().resolve()
    target = Path(os.path.abspath(base / path))
    if base / allowed not in target.parents:
        raise ValueError(f"Output must be under {allowed.as_posix()}/: {path}")
    current = base
    for name in target.relative_to(base).parts:
        current = current / name
        if current.is_symlink():
            raise ValueError(
                f"Output must be under {allowed.as_posix()}/. "
                f"Symlinks are not allowed: {path}"
            )
    return target


def open_output(path, **policy):
    """Open a result file for writing. Never follow a symlink."""
    path = allowed_output(path, **policy)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    return open(os.open(path, flags, 0o644), "w")


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
        from memtorture.controls.gate import methodology_hash, platform_key

        allowed_output(root, control_evidence=control_evidence)
        status_command = ["git", "status", "--porcelain"]
        if control_evidence:
            status_command += ["--", ":/", ":(top,exclude)results/controls"]
        provenance = {
            "harness_commit": commit(),
            "harness_dirty": bool(subprocess.check_output(status_command, text=True)),
        }
        self.root = Path(root)
        self.policy = {"control_evidence": control_evidence}
        self.root.mkdir(parents=True, exist_ok=True)
        self.raw = open_output(self.root / "operations.jsonl", **self.policy)
        self.cells = []
        self.lock = threading.Lock()
        self.manifest = {
            "sponsor": SPONSOR,
            "command": command,
            **provenance,
            "methodology_sha256": methodology_hash(),
            "os": platform.platform(),
            "platform": platform_key(),
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
        for name, data in (
            ("manifest.json", self.manifest),
            ("cells.json", self.cells),
        ):
            with open_output(self.root / name, **self.policy) as output:
                output.write(json.dumps(data, sort_keys=True, indent=2) + "\n")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def render_tables(cells):
    tables = {}
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
        tables[suite] = "\n".join(lines) + "\n"
    return tables
