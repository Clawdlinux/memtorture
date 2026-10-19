import contextlib
import os
import signal
import sys
import time
from unittest.mock import Mock

import pytest

from memtorture.adapters.qdrant import ROOT, VERSION, Qdrant
from memtorture.protocol import ProtocolError, Worker
from memtorture.types import Record, Scope


def test_async_qdrant_delete_is_queued_even_before_upsert_is_visible():
    adapter = Qdrant()
    adapter.wait = False
    operations = []
    scope = Scope("tenant", "session")
    record = Record.make(scope, "key", "not visible yet")

    def points(op, body, wait=None, method="POST"):
        operations.append((op, body, wait, method))
        if op == "" and method == "POST":
            return []
        return {"status": "acknowledged"}

    adapter._points = points
    ack = adapter.write(scope, record)
    adapter.delete(scope, ack.ref)
    assert len(operations) == 2
    assert operations[1] == (
        "/delete",
        {
            "filter": {
                "must": [
                    {"key": "tenant", "match": {"value": "tenant"}},
                    {"key": "session", "match": {"value": "session"}},
                    {"has_id": [ack.ref.id]},
                ]
            }
        },
        False,
        "POST",
    )
    # Apply the queued operations in log order after the visibility delay.
    visible = {}
    for op, body, _, _ in operations:
        if op == "":
            visible.update((p["id"], p) for p in body["points"])
        else:
            condition = body["filter"]["must"]
            for pid in condition[-1]["has_id"]:
                point = visible.get(pid)
                if point and all(
                    point["payload"][item["key"]] == item["match"]["value"]
                    for item in condition[:-1]
                ):
                    visible.pop(pid)
    assert ack.ref.id not in visible


def test_native_qdrant_immediate_async_deletes_stay_deleted(tmp_path):
    artifact = ROOT / ".tools/qdrant" / VERSION / "qdrant"
    if not artifact.is_file():
        pytest.skip(f"Pinned Qdrant artifact is missing: {artifact}")
    adapter = Qdrant()
    adapter.start(tmp_path, "wait=false")
    try:
        scope = Scope("tenant", "immediate-delete")
        refs = []
        for index in range(20):
            record = Record.make(scope, f"key-{index}", f"memory-{index}")
            ack = adapter.write(scope, record)
            adapter.delete(scope, ack.ref)
            refs.append(ack.ref)
        barrier = Record.make(scope, "barrier", "all prior updates are applied")
        adapter.bulk_load(scope, [barrier])
        assert adapter.get(scope, barrier.key) == barrier
        for ref in refs:
            assert adapter.get_by_ref(scope, ref) is None
    finally:
        adapter.stop()


def server_worker_command(mode):
    script = f"""
import json
import subprocess
import sys
import time
children = []
for line in sys.stdin:
    request = json.loads(line)
    op = request["op"]
    if op == "start":
        children = [
            subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ) for _ in range(2)
        ]
        reply = {{"id": request["id"], "result": {{
            "protocol": 1, "capabilities": [], "version": "probe", "notes": "",
            "servers": [
                {{"role": role, "pid": child.pid}}
                for role, child in zip(("storage", "api"), children)
            ],
        }}}}
    elif op == "stop":
        if {mode!r} == "timeout":
            time.sleep(60)
            continue
        reply = {{"id": request["id"], "result": {{}}}}
        if {mode!r} == "error":
            reply = {{"id": request["id"], "error": {{
                "type": "system_error", "message": "stop failed",
            }}}}
    else:
        reply = {{"id": request["id"], "result": None}}
    print(json.dumps(reply), flush=True)
"""
    return [sys.executable, "-c", script]


def pid_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("mode", ["malformed", "error", "timeout"])
def test_failed_stop_kills_every_reported_server(tmp_path, mode):
    worker = Worker(
        "probe", "default", tmp_path, timeout=5, command=server_worker_command(mode)
    )
    if mode == "timeout":
        worker.timeout = 0.1
    pids = list(worker.servers.values())
    try:
        expectation = (
            pytest.raises(ProtocolError)
            if mode == "malformed"
            else contextlib.nullcontext()
        )
        with expectation:
            worker.close()
        assert worker.process.poll() is not None
        assert all(not pid_exists(pid) for pid in pids)
        assert worker.log.closed
    finally:
        if worker.process.poll() is None:
            worker.kill("worker")
        for pid in pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while any(pid_exists(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.01)


def test_preserve_server_keeps_reported_pids(tmp_path):
    worker = Worker(
        "probe", "default", tmp_path, command=server_worker_command("malformed")
    )
    pids = list(worker.servers.values())
    try:
        worker.close(preserve_server=True)
        assert worker.process.poll() is not None
        assert all(pid_exists(pid) for pid in pids)
    finally:
        for pid in pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_exit_reply_timeout_has_margin_and_restores_normal_timeout():
    worker = object.__new__(Worker)
    worker.server_pid = 321
    worker.timeout = 60
    worker.process = Mock()
    worker.process.poll.return_value = None

    def ensure(op, **args):
        assert op == "ensure_server_exited" and args["pid"] == 321
        assert worker.timeout >= 12
        raise TimeoutError("worker-side exit timeout")

    worker.call = ensure
    with pytest.raises(TimeoutError, match="worker-side exit timeout"):
        worker._wait_server_exit()
    assert worker.timeout == 60
