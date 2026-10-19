"""Scan pinned sources before installing. No package lifecycle scripts run."""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from memproof.security import check_scan, tree_hash
from memproof.types import NotSupported

ROOT = Path(__file__).resolve().parents[1]
VERSION = "2025.11.25"
INTEGRITY = (
    "woL22c8U+9bvp+VQVxDatzg2pMr94jSp5Z+1GyTOvGmfZINjypkyA1ID8I/4HgR/"
    "ecATRxFznSK85AESaV+1MA=="
)


def scan(source, report_path, env):
    scanner = shutil.which("skillspector") or str(
        Path.home() / ".local/bin/skillspector"
    )
    with report_path.open("w") as output:
        result = subprocess.run(
            [scanner, "scan", str(source), "--no-llm", "--format", "json"],
            stdout=output,
            env=env,
            timeout=1800,
        )
    check_scan(json.loads(report_path.read_text()))
    if result.returncode:
        raise NotSupported(
            f"SkillSpector exited {result.returncode}. See {report_path}"
        )


def main():
    tool = ROOT / ".tools/mcp"
    scratch = ROOT / ".scratch/mcp-2025.11.25"
    scratch.mkdir(parents=True, exist_ok=True)
    tool.mkdir(parents=True, exist_ok=True)
    env = os.environ | {
        "TMPDIR": str(ROOT / ".scratch"),
        "npm_config_cache": str(ROOT / ".scratch/npm-cache"),
    }
    gate = tool / "attestation.json"
    gate.unlink(missing_ok=True)
    try:
        subprocess.run(
            [
                "npm",
                "pack",
                f"@modelcontextprotocol/server-memory@{VERSION}",
                "--ignore-scripts",
                "--pack-destination",
                str(scratch),
                "--json",
            ],
            check=True,
            env=env,
            stdout=subprocess.DEVNULL,
        )
        tarball = scratch / f"modelcontextprotocol-server-memory-{VERSION}.tgz"
        digest = base64.b64encode(
            hashlib.sha512(tarball.read_bytes()).digest()
        ).decode()
        if digest != INTEGRITY:
            raise NotSupported("MCP tarball does not match the pinned SHA-512")
        extracted = scratch / "package"
        if extracted.exists():
            shutil.rmtree(extracted)
        with tarfile.open(tarball) as bundle:
            bundle.extractall(scratch, filter="data")
        scan(extracted, tool / "source-scan.json", env)
        for name in ("package.json", "package-lock.json"):
            shutil.copyfile(ROOT / "scripts/mcp" / name, tool / name)
        subprocess.run(
            ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=tool,
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        # Dependencies are also inspected before the first Node process starts.
        scan(tool / "node_modules", tool / "dependency-scan.json", env)
        installed = tool / "node_modules/@modelcontextprotocol/server-memory"
        for path in extracted.rglob("*"):
            if (
                path.is_file()
                and path.read_bytes()
                != (installed / path.relative_to(extracted)).read_bytes()
            ):
                raise NotSupported(f"Installed source differs from scan: {path.name}")
        attestation = {
            "version": VERSION,
            "source_sha512": INTEGRITY,
            "tree_sha256": tree_hash(tool / "node_modules"),
        }
        gate.write_text(json.dumps(attestation, indent=2) + "\n")
        (tool / "blocked.txt").unlink(missing_ok=True)
        print(f"MCP {VERSION}: SAFE. Source and dependencies fully inspected.")
    except (
        NotSupported,
        OSError,
        ValueError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        reason = str(exc)
        (tool / "blocked.txt").write_text(reason + "\n")
        if (tool / "node_modules").exists():
            shutil.rmtree(tool / "node_modules")
        print(f"MCP adapter disabled: {reason}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
