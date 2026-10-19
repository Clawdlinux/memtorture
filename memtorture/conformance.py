"""Replay public protocol transcripts without third-party packages."""

import json
import shlex
import shutil
import time
import uuid
from pathlib import Path

from memtorture.protocol import ProtocolError, Worker, decode, encode, validate_reply
from memtorture.types import Ack, NotSupported, Record
from memtorture.wire import decode_args

FIXTURES = Path(__file__).resolve().parents[1] / "docs/protocol/v1"


def cases(name):
    return [
        json.loads(line)
        for line in (FIXTURES / name).read_text().splitlines()
        if line.strip()
    ]


def check_malformed():
    fixtures = cases("malformed.jsonl")
    for case in fixtures:
        accepted = True
        try:
            reply = case.get("reply")
            if "raw_hex" in case:
                reply = json.loads(bytes.fromhex(case["raw_hex"]).decode("utf-8"))
            validate_reply(reply, 1, case["op"], decode_args(case["args"]))
            decode(reply.get("result"))
        except (ProtocolError, ValueError, UnicodeError):
            accepted = False
        if accepted != case["valid"]:
            raise ProtocolError(f"Malformed fixture disagrees: {case['name']}")
    return len(fixtures)


def run_conformance(command, *, config="default", workdir=None):
    argv = shlex.split(command)
    if not argv:
        raise ValueError("--command must contain an executable")
    malformed = check_malformed()
    path = Path(workdir) if workdir else Path(".conformance-work") / uuid.uuid4().hex
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=False)
    records, refs = {}, {}
    passed, skipped = 0, []
    succeeded = {"start"}

    def expand(value):
        if isinstance(value, str) and value.startswith("$ref:"):
            name = value.removeprefix("$ref:")
            if name not in refs:
                raise NotSupported(f"No native reference returned for {name}")
            return refs[name]
        if isinstance(value, str) and value.startswith("$record:"):
            return records[value.removeprefix("$record:")]
        if isinstance(value, list):
            return [expand(v) for v in value]
        if isinstance(value, dict):
            return {k: expand(v) for k, v in value.items()}
        return value

    try:
        with Worker("conformance", config, path, command=argv, timeout=10) as worker:
            for case in cases("golden.jsonl"):
                request = case["request"]
                op = request["op"]
                if op == "start":
                    passed += 1
                    continue
                if op == "stop":
                    for role in worker.servers:
                        worker.kill(f"server:{role}")
                        passed += 1
                if not set(case.get("requires", ())) <= worker.capabilities | succeeded:
                    skipped.append(f"{request['id']}:{op}: prerequisite unsupported")
                    continue
                try:
                    args = decode_args(expand(request["args"]))
                except NotSupported:
                    if op in worker.capabilities:
                        raise ProtocolError(
                            f"{op} advertised without a usable native ref"
                        ) from None
                    skipped.append(f"{request['id']}:{op}: no ref")
                    continue
                for record in (
                    [args["record"]] if "record" in args else args.get("records", [])
                ):
                    if not record.valid():
                        raise ProtocolError("Golden record digest mismatch")
                    records[record.id] = encode(record)
                try:
                    result = worker.call(op, **args)
                except NotSupported:
                    if op in worker.capabilities or op in {
                        "capabilities",
                        "write",
                        "stop",
                    }:
                        raise ProtocolError(
                            f"{op} incorrectly returned not_supported"
                        ) from None
                    skipped.append(f"{request['id']}:{op}: not_supported")
                    continue
                except RuntimeError as exc:
                    expected = case["reply"].get("error")
                    if expected and str(exc).startswith(expected["type"] + ":"):
                        passed += 1
                        continue
                    raise
                if "error" in case["reply"]:
                    raise ProtocolError(f"{op} accepted an unreported server PID")
                if isinstance(result, Ack) and op in {"write", "upsert"}:
                    if result.dropped:
                        raise ProtocolError(f"{op} dropped the golden record")
                    if result.ref is not None:
                        refs[result.id] = result.ref.id
                    if "get_by_ref" in worker.capabilities and result.ref is None:
                        raise ProtocolError("get_by_ref requires native write refs")
                match = case.get("match")
                if match == "capabilities":
                    if set(result) != worker.capabilities:
                        raise ProtocolError("Capabilities changed after start")
                elif match != "shape":
                    value = case["reply"]["result"]
                    if isinstance(result, Ack) and result.ref is None and op == "write":
                        value = value | {"ref": None}
                    expected = decode(expand(value))
                    deadline = time.monotonic() + 10
                    while (
                        result != expected
                        and op
                        in {"get", "get_by_ref", "read_recent", "list_prefix", "search"}
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                        result = worker.call(op, **args)
                    if result != expected:
                        raise ProtocolError(
                            f"{op} disagrees with golden reply {request['id']}"
                        )
                    rows = result if isinstance(result, list) else [result]
                    if any(isinstance(r, Record) and not r.valid() for r in rows):
                        raise ProtocolError(f"{op} returned a torn golden record")
                passed += 1
                succeeded.add(op)
        return {"passed": passed, "skipped": skipped, "malformed": malformed}
    finally:
        shutil.rmtree(path)
        if workdir is None:
            try:
                path.parent.rmdir()
            except OSError:
                pass
