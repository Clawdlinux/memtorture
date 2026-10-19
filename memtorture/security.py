import hashlib
from pathlib import Path

from memtorture.types import NotSupported


def check_scan(report):
    assessment = report.get("risk_assessment", {})
    coverage = report.get("analysis_completeness", {})
    verdict = assessment.get("recommendation", "scan error")
    if (
        verdict != "SAFE"
        or not report.get("execution_successful")
        or not coverage.get("is_complete")
        or coverage.get("entirely_uninspected_files", 0)
        or coverage.get("partially_inspected_files", 0)
    ):
        raise NotSupported(
            f"MCP security gate: {verdict}; complete={coverage.get('is_complete')}; "
            f"fully inspected={coverage.get('fully_inspected_files', 0)}/"
            f"{coverage.get('total_components', '?')}"
        )


def tree_hash(root):
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()
