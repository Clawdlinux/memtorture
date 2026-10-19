"""Download a pinned native binary. Verify its archive before extraction."""

import hashlib
import platform
import subprocess
import tarfile
from pathlib import Path

VERSION = "1.16.3"
ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {
    ("Darwin", "arm64"): "aarch64-apple-darwin",
    ("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
}


def verify(path, expected):
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError(f"SHA-256 mismatch: expected {expected}, got {actual}")


def main():
    target = PLATFORMS.get((platform.system(), platform.machine()))
    if not target:
        raise SystemExit("Only macOS arm64 and Linux x86_64 are pinned.")
    name = f"qdrant-{target}.tar.gz"
    checksums = {
        parts[1]: parts[0]
        for line in (ROOT / "scripts/qdrant.sha256").read_text().splitlines()
        if not line.startswith("#") and (parts := line.split())
    }
    destination = ROOT / ".tools/qdrant" / VERSION
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / name
    url = f"https://github.com/qdrant/qdrant/releases/download/v{VERSION}/{name}"
    subprocess.run(["curl", "-fsSL", url, "-o", str(archive)], check=True)
    verify(archive, checksums[name])
    with tarfile.open(archive) as bundle:
        members = [m for m in bundle.getmembers() if Path(m.name).name == "qdrant"]
        if len(members) != 1 or not members[0].isfile():
            raise ValueError("Expected one regular Qdrant binary")
        data = bundle.extractfile(members[0]).read()
    binary = destination / "qdrant"
    binary.write_bytes(data)
    binary.chmod(0o755)
    (destination / "binary.sha256").write_text(hashlib.sha256(data).hexdigest() + "\n")
    print(f"Qdrant {VERSION}: SHA-256 {checksums[name]}")
    print(binary)


if __name__ == "__main__":
    main()
