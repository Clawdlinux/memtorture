import os
import signal
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from memproof.adapters.markdown import Markdown
from memproof.adapters.qdrant import Qdrant
from memproof.protocol import Worker
from memproof.types import Record, Scope
from tests.test_protocol import Probe


class ServerProbe(Probe):
    def start(self, workdir, config):
        super().start(workdir, config)
        self.child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
            ]
        )
        self.server_pid = self.child.pid

    def stop(self, target="all", kill=False):
        if self.child.poll() is None:
            self.child.terminate()
        self.child.wait(timeout=10)


@pytest.mark.parametrize("config", ["append", "rewrite"])
def test_markdown_bulk_batches_never_read_or_rewrite_existing_rows(
    tmp_path, monkeypatch, config
):
    adapter = Markdown()
    adapter.start(tmp_path, config)
    scope = Scope("t", "s")
    first = Record.make(scope, "first", "memory")
    second = Record.make(scope, "second", "memory")
    adapter.write(scope, first)
    prefix = adapter.path.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("Untimed bulk setup must append without scanning")

    monkeypatch.setattr(adapter, "_all", forbidden)
    adapter.bulk_load(scope, [second])
    assert adapter.path.read_bytes().startswith(prefix)
    assert len(adapter.path.read_text().splitlines()) == 2


def bare_worker():
    worker = object.__new__(Worker)
    worker.server_pid = 321
    worker.timeout = 10
    worker.process = Mock()
    worker.process.poll.return_value = 0
    return worker


def test_server_kill_waits_for_pid_disappearance(monkeypatch):
    worker = bare_worker()
    probes = []

    def kill(pid, sig):
        assert pid == 321
        if sig == 0:
            probes.append(pid)
            if len(probes) == 3:
                raise ProcessLookupError
        else:
            assert sig == signal.SIGKILL

    monkeypatch.setattr("memproof.protocol.os.kill", kill)
    monkeypatch.setattr("memproof.protocol.time.sleep", lambda _: None)
    worker.kill("server")
    assert len(probes) == 3


def test_server_kill_wait_is_bounded(monkeypatch):
    worker = bare_worker()
    clock = iter(range(100))
    monkeypatch.setattr("memproof.protocol.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("memproof.protocol.time.sleep", lambda _: None)
    monkeypatch.setattr("memproof.protocol.os.kill", lambda *args: None)
    with pytest.raises(TimeoutError, match="server"):
        worker.kill("server")


def test_server_child_is_reaped_before_restart(tmp_path):
    command = [
        sys.executable,
        "-c",
        "from tests.test_review_lifecycle import ServerProbe; "
        "from memproof.worker import serve; serve(ServerProbe())",
    ]
    with Worker("probe", "default", tmp_path, command=command) as worker:
        worker.kill("server")
        with pytest.raises(ProcessLookupError):
            os.kill(worker.server_pid, 0)


@pytest.mark.parametrize("sequence", [0, 42])
def test_qdrant_prefers_harness_sequence(monkeypatch, sequence):
    record = Record.make(Scope("t", "s"), "key", "memory")
    sequenced = SimpleNamespace(**vars(record), sequence=sequence)

    def forbidden_clock():
        raise AssertionError("Harness order must not depend on wall clock")

    monkeypatch.setattr("memproof.adapters.qdrant.time.time_ns", forbidden_clock)
    assert Qdrant._point(sequenced)["payload"]["_order"] == sequence


def test_qdrant_wall_clock_fallback(monkeypatch):
    record = Record.make(Scope("t", "s"), "key", "memory")
    monkeypatch.setattr("memproof.adapters.qdrant.time.time_ns", lambda: 123456)
    assert Qdrant._point(record)["payload"]["_order"] == 123456
