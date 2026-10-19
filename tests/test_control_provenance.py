import pytest

from memtorture import results


@pytest.mark.parametrize(
    "control_evidence,source_dirty,expected",
    [(True, False, False), (True, True, True), (False, False, True)],
)
def test_control_provenance_ignores_only_generated_evidence(
    tmp_path, monkeypatch, control_evidence, source_dirty, expected
):
    directory = tmp_path / "output"
    status_seen = []

    def git_output(command, **kwargs):
        assert not directory.exists(), "Capture provenance before opening output files"
        if command[1] == "rev-parse":
            return "a" * 40 + "\n"
        assert command[:3] == ["git", "status", "--porcelain"]
        status_seen.append(command)
        if source_dirty:
            return " M memtorture/embed.py\n"
        if ":(top,exclude)results/controls" in command:
            return ""
        return " M results/controls/full/operations.jsonl\n"

    monkeypatch.setattr(results.subprocess, "check_output", git_output)
    monkeypatch.setattr(results, "hardware", lambda: {})
    monkeypatch.setattr(results.platform, "platform", lambda: "test-os")
    with results.ResultWriter(
        directory, ["test"], control_evidence=control_evidence
    ) as writer:
        assert writer.manifest["harness_commit"] == "a" * 40
        assert writer.manifest["harness_dirty"] is expected
    assert bool(":(top,exclude)results/controls" in status_seen[0]) is control_evidence
