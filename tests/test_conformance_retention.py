import os
import shlex
import sys
from pathlib import Path

import pytest

from memtorture import conformance
from memtorture.protocol import ProtocolError


def seed_failures(root, count):
    root.mkdir()
    for index in range(count):
        directory = root / f"failed-{index:02d}"
        directory.mkdir()
        (directory / "worker.stderr.log").write_text(f"failure {index}\n")
        os.utime(directory, (index + 1, index + 1))


def test_run_start_keeps_only_newest_failures_and_preserves_active_runs(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / ".conformance-work"
    seed_failures(root, 13)
    crashed = root / "failed-00"
    (crashed / ".running").write_text("2147483647")
    os.utime(crashed, (0, 0))
    for index, marker in ((1, ""), (2, "-1")):
        interrupted = root / f"failed-{index:02d}"
        (interrupted / ".running").write_text(marker)
        os.utime(interrupted, (index, index))
    active = root / "active"
    active.mkdir()
    (active / ".running").write_text(str(os.getpid()))
    os.utime(active, (0, 0))
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    result = conformance.run_conformance(
        shlex.join([sys.executable, "-m", "memtorture.worker", "sqlite"]),
        config="synchronous=NORMAL",
    )
    assert result["passed"] > 0
    assert {p.name for p in root.glob("failed-*")} == {
        f"failed-{index:02d}" for index in range(3, 13)
    }
    assert active.is_dir()
    assert outside.is_dir() and (root / "linked").is_symlink()


def test_new_failure_is_kept_without_exceeding_retention_limit(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / ".conformance-work"
    seed_failures(root, 10)
    with pytest.raises(ProtocolError):
        conformance.run_conformance(shlex.join([sys.executable, "-c", "print(123)"]))
    kept = next(
        line.removeprefix("Work directory kept: ")
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("Work directory kept: ")
    )
    assert Path(kept).is_dir()
    assert (Path(kept) / "worker.stderr.log").is_file()
    assert len(list(root.iterdir())) == 10
    assert not (root / "failed-00").exists()


def test_in_progress_marker_exists_before_worker_start_and_success_cleans_it(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    original = conformance.Worker

    class ObservedWorker(original):
        def __init__(self, system, config, workdir, **kwargs):
            assert (Path(workdir) / ".running").read_text() == str(os.getpid())
            super().__init__(system, config, workdir, **kwargs)

    monkeypatch.setattr(conformance, "Worker", ObservedWorker)
    conformance.run_conformance(
        shlex.join([sys.executable, "-m", "memtorture.worker", "sqlite"]),
        config="synchronous=NORMAL",
    )
    assert not (tmp_path / ".conformance-work").exists()
