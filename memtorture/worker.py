import contextlib
import inspect
import json
import os
import sys
import time
from pathlib import Path

from memtorture.protocol import encode
from memtorture.types import NotSupported
from memtorture.wire import (
    OPERATIONS,
    ProtocolError,
    decode_args,
    require,
    validate_request,
)


def ensure_server_exited(pid, timeout=10):
    deadline = time.monotonic() + timeout
    while True:
        try:
            reaped, _ = os.waitpid(pid, os.WNOHANG)
            if reaped:
                return
        except ChildProcessError:
            pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Server {pid} did not exit")
        time.sleep(0.01)


def serve(adapter, source=None, output=None):
    source = source or sys.stdin
    output = output or sys.stdout
    for line in source:
        request = {}
        try:
            request = json.loads(line)
            validate_request(request)
            op, args = request["op"], decode_args(request["args"])
            # Adapter logs cannot corrupt protocol stdout.
            with contextlib.redirect_stdout(sys.stderr):
                if op == "start":
                    adapter.start(Path(args["workdir"]), args["config"])
                    servers = getattr(adapter, "servers", None)
                    if servers is None:
                        pid = getattr(adapter, "server_pid", None)
                        servers = [{"role": "storage", "pid": pid}] if pid else []
                    result = {
                        "protocol": 1,
                        "capabilities": sorted(adapter.capabilities()),
                        "servers": servers,
                        "version": getattr(adapter, "version", "unreported"),
                        "notes": getattr(adapter, "notes", ""),
                        "runtime_versions": getattr(adapter, "runtime_versions", {}),
                    }
                elif op == "bulk_load":
                    method = getattr(adapter, "bulk_load", None)
                    if method:
                        result = method(scope=args["scope"], records=args["records"])
                    else:
                        for record in args["records"]:
                            adapter.write(args["scope"], record)
                        result = None
                elif op == "ensure_server_exited":
                    pid = args["pid"]
                    require(
                        type(pid) is int and pid > 0, "Expected positive server PID"
                    )
                    known = getattr(adapter, "servers", None) or [
                        {"pid": getattr(adapter, "server_pid", None)}
                    ]
                    require(
                        any(s["pid"] == pid for s in known), "Unreported server PID"
                    )
                    ensure_server_exited(pid)
                    result = None
                elif op in OPERATIONS:
                    method = getattr(adapter, op, None)
                    if method is None:
                        raise NotSupported(f"{op} is not supported")
                    parameters = inspect.signature(method).parameters
                    result = method(
                        **{k: v for k, v in args.items() if k in parameters}
                    )
                else:
                    raise NotSupported(f"Unknown operation: {op}")
            reply = {"id": request["id"], "result": encode(result)}
        except Exception as exc:
            error_type = "system_error"
            if isinstance(exc, NotSupported):
                error_type = "not_supported"
            elif isinstance(exc, (ValueError, TypeError, KeyError, ProtocolError)):
                error_type = "invalid_request"
            elif isinstance(exc, TimeoutError):
                error_type = "timeout"
            reply = {
                "id": request.get("id") if isinstance(request, dict) else None,
                "error": {"type": error_type, "message": str(exc)},
            }
        output.write(json.dumps(reply, ensure_ascii=False) + "\n")
        output.flush()


def main():
    from memtorture.adapters import load_adapter

    serve(load_adapter(sys.argv[1]))


if __name__ == "__main__":
    main()
