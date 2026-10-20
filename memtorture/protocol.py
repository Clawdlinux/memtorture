import contextlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from dataclasses import fields, is_dataclass, replace
from pathlib import Path

from memtorture.types import Ack, NotSupported, Record, Ref
from memtorture.wire import (
    OPERATIONS,
    ProtocolError,
    decode,
    decode_args,
    validate_reply,
)

__all__ = [
    "OPERATIONS",
    "ProtocolError",
    "Worker",
    "decode",
    "encode",
    "parse_reply",
    "validate_reply",
    "worker_command",
]
ROOT = Path(__file__).resolve().parents[1]


def parse_reply(line, token, op, args):
    try:
        reply = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ProtocolError("Worker stdout must contain UTF-8 JSONL only") from exc
    validate_reply(reply, token, op, args)
    return reply


def encode(value):
    if is_dataclass(value):
        tags = {Record: "record", Ack: "ack", Ref: "ref"}
        tag = {"kind": tags[type(value)]} if type(value) in tags else {}
        return tag | {f.name: encode(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [encode(v) for v in value]
    return value


def worker_command(system):
    if not system or any(
        c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in system
    ):
        raise NotSupported(f"Invalid system name: {system}")
    path = ROOT / "systems" / system / "manifest.json"
    try:
        command = json.loads(path.read_text())["command"]
    except (OSError, ValueError, KeyError) as exc:
        raise NotSupported(f"Missing worker manifest: {path}") from exc
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(arg, str) and arg for arg in command)
    ):
        raise ProtocolError(f"Invalid command in {path}")
    return [sys.executable if arg == "{python}" else arg for arg in command]


class Worker:
    """JSONL client. Only receive() can add a write to the harness ack set."""

    def __init__(self, system, config, workdir, *, command=None, timeout=60, cwd=None):
        self.workdir = Path(workdir).resolve() if cwd is not None else Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.acked = {}
        self.refs = {}
        self.pending = {}
        self.buffer = b""
        self.sequence = 0
        self.last_sent_ns = 0
        self.last_ack_ns = 0
        self.stopped = False
        self.closed = False
        self.server_pid = None
        self.log = (self.workdir / "worker.stderr.log").open("ab")
        self.process = subprocess.Popen(
            command or worker_command(system),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            bufsize=0,
            cwd=cwd,
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            self.info = self.call("start", workdir=self.workdir, config=config)
            self.servers = {s["role"]: s["pid"] for s in self.info["servers"]}
            self.server_pid = self.servers.get("storage")
            self.capabilities = set(self.info["capabilities"])
        except BaseException:
            self.close()
            raise

    def send(self, op, *, raw_args=None, **kwargs):
        from memtorture.embed import embed

        if self.pending:
            raise RuntimeError("Read the outstanding reply before sending another")
        self.sequence += 1
        token = self.sequence
        if op in {"write", "upsert", "bulk_load"}:
            name = "records" if op == "bulk_load" else "record"
            records = kwargs[name] if name == "records" else [kwargs[name]]
            records = [
                r if r.vector is not None else replace(r, vector=embed(r.text))
                for r in records
            ]
            kwargs[name] = records if name == "records" else records[0]
        if op == "search":
            kwargs["vector"] = embed(kwargs["query"])
        wire_args = encode(kwargs) if raw_args is None else dict(raw_args)
        if raw_args is not None:
            if op in {"write", "upsert"}:
                wire_args["record"] = raw_args["record"] | {
                    "vector": kwargs["record"].vector
                }
            elif op == "bulk_load":
                wire_args["records"] = [
                    raw | {"vector": record.vector}
                    for raw, record in zip(
                        raw_args["records"], kwargs["records"], strict=True
                    )
                ]
            elif op == "search":
                wire_args["vector"] = kwargs["vector"]
        request = {"id": token, "op": op, "args": wire_args}
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
        op, kwargs = self.pending.pop(token)
        reply = parse_reply(line, token, op, kwargs)
        if b"\n" in self.buffer:
            raise ProtocolError("Unsolicited worker stdout line")
        if "error" in reply:
            error = reply["error"]
            if error["type"] == "not_supported":
                raise NotSupported(error["message"])
            if error["type"] == "timeout":
                raise TimeoutError(error["message"])
            raise RuntimeError(f"{error['type']}: {error['message']}")
        result = decode(reply["result"])
        if op == "stop":
            self.stopped = True
        if op in {"write", "upsert"} and isinstance(result, Ack):
            self.last_ack_ns = read_ns
            if not result.dropped:
                record = kwargs["record"]
                self.acked[record.id] = record
                if result.ref is not None:
                    self.refs[record.id] = result.ref
        return result

    def call(self, op, **kwargs):
        return self.receive(self.send(op, **kwargs))

    def call_raw(self, op, args):
        return self.receive(self.send(op, raw_args=args, **decode_args(args)))

    def kill(self, target):
        servers = getattr(self, "servers", {})
        if target.startswith("server:"):
            role = target.split(":", 1)[1]
            if role not in servers:
                raise NotSupported(f"No server role: {role}")
            pids = [servers[role]]
        elif target in {"server", "both"}:
            pids = list(servers.values()) or (
                [self.server_pid] if self.server_pid else []
            )
        elif target == "worker":
            pids = []
        else:
            raise ValueError(f"Unknown fault target: {target}")
        if target != "worker":
            if not pids:
                raise NotSupported("This system has no storage server PID")
        for pid in pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        if target in {"worker", "both"} and self.process.poll() is None:
            os.kill(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=10)
        for pid in pids:
            self._wait_server_exit(pid)

    def _wait_server_exit(self, pid=None):
        pid = pid or self.server_pid
        deadline = time.monotonic() + 10
        if self.process.poll() is None:
            original_timeout = self.timeout
            self.timeout = 12
            try:
                self.call("ensure_server_exited", pid=pid)
            finally:
                self.timeout = original_timeout
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Storage server {pid} did not exit")
            time.sleep(0.01)

    def close(self, *, preserve_server=False):
        if self.closed:
            return
        clean_stop = False
        try:
            if self.process.poll() is None:
                try:
                    if preserve_server:
                        self.kill("worker")
                    else:
                        if not self.stopped:
                            self.call("stop")
                        self.process.stdin.close()
                        self.process.wait(timeout=10)
                except ProtocolError:
                    raise
                except (OSError, RuntimeError, TimeoutError, subprocess.TimeoutExpired):
                    self.kill("worker")
            if self.stopped and not preserve_server:
                os.set_blocking(self.process.stdout.fileno(), False)
                try:
                    trailing = os.read(self.process.stdout.fileno(), 65536)
                except BlockingIOError:
                    trailing = b""
                if self.buffer or trailing:
                    raise ProtocolError("Unsolicited worker stdout after stop")
                clean_stop = self.process.returncode == 0
        finally:
            pids = (
                list(getattr(self, "servers", {}).values())
                if not clean_stop and not preserve_server
                else []
            )
            try:
                try:
                    for pid in pids:
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(pid, signal.SIGKILL)
                finally:
                    if self.process.poll() is None:
                        self.kill("worker")
                for pid in pids:
                    self._wait_server_exit(pid)
            finally:
                self.selector.close()
                for stream in (self.process.stdin, self.process.stdout, self.log):
                    stream.close()
                self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
