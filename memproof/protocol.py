import contextlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from dataclasses import fields, is_dataclass
from pathlib import Path

from memproof.types import Ack, NotSupported, QueryContext, Record, Ref, Scope

TYPES = {cls.__name__: cls for cls in (Ack, QueryContext, Record, Ref, Scope)}


def encode(value):
    if is_dataclass(value):
        return {"_type": type(value).__name__} | {
            f.name: encode(getattr(value, f.name)) for f in fields(value)
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [encode(v) for v in value]
    return value


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        body = {k: decode(v) for k, v in value.items() if k != "_type"}
        return TYPES[value["_type"]](**body) if "_type" in value else body
    return value


class Worker:
    """JSONL client. Only receive() can add a write to the harness ack set."""

    def __init__(self, system, config, workdir, *, command=None, timeout=60):
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.acked = {}
        self.pending = {}
        self.buffer = b""
        self.sequence = 0
        self.last_sent_ns = 0
        self.last_ack_ns = 0
        self.server_pid = None
        self.log = (self.workdir / "worker.stderr.log").open("ab")
        self.process = subprocess.Popen(
            command or [sys.executable, "-m", "memproof.worker", system],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            bufsize=0,
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            self.info = self.call("start", workdir=self.workdir, config=config)
            self.server_pid = self.info.get("server_pid")
            self.capabilities = set(self.info["capabilities"])
        except BaseException:
            self.close()
            raise

    def send(self, op, **kwargs):
        if self.pending:
            raise RuntimeError("Read the outstanding reply before sending another")
        self.sequence += 1
        token = self.sequence
        request = {"id": token, "op": op, "args": encode(kwargs)}
        self.pending[token] = (op, kwargs)
        self.last_sent_ns = time.monotonic_ns()
        data = (json.dumps(request, ensure_ascii=False) + "\n").encode()
        view = memoryview(data)
        while view:
            sent = os.write(self.process.stdin.fileno(), view)
            view = view[sent:]
        return token

    def receive(self, token):
        deadline = time.monotonic() + self.timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError("Worker reply timed out")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError(
                    f"Worker exited. See {self.workdir}/worker.stderr.log"
                )
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        read_ns = time.monotonic_ns()
        reply = json.loads(line)
        if reply["id"] != token:
            raise RuntimeError("Worker reply does not match request")
        op, kwargs = self.pending.pop(token)
        if "error" in reply:
            error = reply["error"]
            if error["type"] == "NotSupported":
                raise NotSupported(error["message"])
            raise RuntimeError(f"{error['type']}: {error['message']}")
        result = decode(reply["result"])
        if op in {"write", "upsert"} and isinstance(result, Ack):
            self.last_ack_ns = read_ns
            if not result.dropped:
                record = (
                    kwargs["record"]
                    if op == "write"
                    else Record.from_value(
                        kwargs["scope"], kwargs["key"], kwargs["value"]
                    )
                )
                if result.id != record.id:
                    raise RuntimeError("Ack does not match the written payload")
                self.acked[record.id] = record
        return result

    def call(self, op, **kwargs):
        return self.receive(self.send(op, **kwargs))

    def kill(self, target):
        if target not in {"worker", "server", "both"}:
            raise ValueError(f"Unknown fault target: {target}")
        if target in {"server", "both"}:
            if self.server_pid is None:
                raise NotSupported("This system has no storage server PID")
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.server_pid, signal.SIGKILL)
        if target in {"worker", "both"} and self.process.poll() is None:
            os.kill(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=10)

    def close(self, *, preserve_server=False):
        if self.process.poll() is None:
            try:
                if preserve_server:
                    self.kill("worker")
                else:
                    self.call("stop")
                    self.process.stdin.close()
                    self.process.wait(timeout=10)
            except (OSError, RuntimeError, TimeoutError, subprocess.TimeoutExpired):
                self.kill("worker")
        self.selector.close()
        for stream in (self.process.stdin, self.process.stdout, self.log):
            stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
