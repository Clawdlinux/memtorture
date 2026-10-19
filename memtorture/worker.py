import json
import os
import sys
from pathlib import Path

from memtorture.protocol import decode, encode
from memtorture.types import NotSupported

METHODS = {
    "write",
    "read_recent",
    "get",
    "get_by_id",
    "list_prefix",
    "search",
    "upsert",
    "delete",
    "retrieve_for",
    "data_dirs",
    "capabilities",
    "stop",
}


def serve(adapter, source=None, output=None):
    source = source or sys.stdin
    output = output or sys.stdout
    for line in source:
        request = {}
        try:
            request = json.loads(line)
            op, args = request["op"], decode(request["args"])
            if op == "start":
                adapter.start(Path(args["workdir"]), args["config"])
                result = {
                    "capabilities": sorted(adapter.capabilities()),
                    "server_pid": getattr(adapter, "server_pid", None),
                    "version": getattr(adapter, "version", "unreported"),
                    "notes": getattr(adapter, "notes", ""),
                    "runtime_versions": getattr(adapter, "runtime_versions", {}),
                }
            elif op == "bulk_load":
                method = getattr(adapter, "bulk_load", None)
                if method:
                    result = method(**args)
                else:
                    for record in args["records"]:
                        adapter.write(args["scope"], record)
                    result = None
            elif op == "reap_server":
                pid = args["pid"]
                if pid <= 0:
                    raise ValueError("Expected a positive storage server PID")
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
                result = None
            elif op in METHODS:
                method = getattr(adapter, op, None)
                if method is None:
                    raise NotSupported(f"{op} is not supported")
                result = method(**args)
            else:
                raise NotSupported(f"Unknown operation: {op}")
            reply = {"id": request["id"], "result": encode(result)}
        except Exception as exc:
            reply = {
                "id": request.get("id"),
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        output.write(json.dumps(reply, ensure_ascii=False) + "\n")
        output.flush()


def main():
    from memtorture.adapters import load_adapter

    serve(load_adapter(sys.argv[1]))


if __name__ == "__main__":
    main()
