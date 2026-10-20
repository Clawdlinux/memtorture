from contextlib import ExitStack, nullcontext

import pytest

from memtorture.adapters import load_adapter
from memtorture.adapters.qdrant import VERSION
from memtorture.controls import gate
from memtorture.protocol import Worker
from memtorture.types import NotSupported
from tests.helpers import (
    NODE,
    ROOT,
    GitProvenance,
    LastRowWorker,
    cleanup_worker,
    probe_command,
    stub_concurrent,
    stub_control_worker,
    stub_forget,
    stub_kill,
    stub_recall,
    stub_supersede,
)


def pytest_addoption(parser):
    parser.addoption(
        "--slow", action="store_true", help="Run opt-in native suite scenarios."
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--slow"):
        for item in items:
            if "slow" in item.keywords:
                item.add_marker(
                    pytest.mark.skip(reason="Use --slow for native suite scenarios")
                )


def pytest_configure(config):
    if config.option.basetemp is None:
        workspace = ROOT / ".pytest_cache"
        workspace.mkdir(exist_ok=True)
        config.option.basetemp = str(workspace / "work")


@pytest.fixture
def node():
    if NODE is None:
        pytest.skip("Node is missing")
    return NODE


@pytest.fixture
def available():
    def check(name):
        if name == "mcp-memory":
            from memtorture.adapters.mcp_memory import BLOCK_REASON

            pytest.skip(BLOCK_REASON)
        if name == "qdrant":
            artifact = ROOT / ".tools/qdrant" / VERSION / "qdrant"
            if not artifact.is_file():
                pytest.skip(f"Pinned Qdrant artifact is missing: {artifact}")

    return check


@pytest.fixture
def adapter_factory(tmp_path, available):
    with ExitStack() as stack:

        def start(name, config):
            available(name)
            adapter = load_adapter(name)
            adapter.start(tmp_path, config)
            stack.callback(adapter.stop)
            return adapter

        yield start


@pytest.fixture
def worker_factory(tmp_path):
    workers = []

    def start(config="default", *, name="Probe", command=None, timeout=5):
        worker = Worker(
            "probe",
            config,
            tmp_path / str(len(workers)),
            timeout=timeout,
            command=command or probe_command(name, nonzero=config == "nonzero"),
            cwd=ROOT,
        )
        workers.append(worker)
        return worker

    yield start
    for worker in reversed(workers):
        cleanup_worker(worker)


@pytest.fixture
def keyed_worker(tmp_path):
    def start(system):
        if system == "last-row":
            return nullcontext(LastRowWorker())
        return Worker(system, "default", tmp_path)

    return start


@pytest.fixture
def stub_controls(monkeypatch):
    monkeypatch.setattr(gate, "recall_delay", stub_recall)
    monkeypatch.setattr(gate, "process_kill", stub_kill)
    monkeypatch.setattr(gate, "concurrent", stub_concurrent)
    monkeypatch.setattr(gate, "Worker", stub_control_worker)
    monkeypatch.setattr(gate, "check_supersede_by_key", stub_supersede)
    monkeypatch.setattr(gate, "forget", stub_forget)
    monkeypatch.setattr(gate, "forget_residue", stub_forget)


@pytest.fixture
def blocked_controls(monkeypatch, tmp_path):
    def blocked(suite):
        raise NotSupported("controls missing")

    monkeypatch.setattr("memtorture.runner.require_controls", blocked)
    monkeypatch.setattr("memtorture.runner.DEFAULT_ROOT", tmp_path / "controls")


@pytest.fixture
def authorized_controls(monkeypatch, tmp_path):
    monkeypatch.setattr("memtorture.runner.require_controls", lambda suite: None)
    monkeypatch.setattr("memtorture.runner.DEFAULT_ROOT", tmp_path / "controls")


@pytest.fixture
def provenance(monkeypatch):
    def configure(directory, source_dirty):
        git = GitProvenance(directory, source_dirty)
        monkeypatch.setattr("memtorture.results.subprocess.check_output", git)
        monkeypatch.setattr("memtorture.results.hardware", lambda: {"machine": "test"})
        monkeypatch.setattr("memtorture.results.platform.platform", lambda: "test-os")
        return git

    return configure


@pytest.fixture
def encoded_worker(monkeypatch):
    def start(system, config, workdir):
        return Worker(
            system, config, workdir, command=probe_command("EncodedMemory"), cwd=ROOT
        )

    monkeypatch.setattr("memtorture.suites.Worker", start)
