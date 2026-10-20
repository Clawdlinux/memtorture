"""Replay public protocol transcripts without third-party packages."""

import contextlib
import json
import os
import shlex
import shutil
import time
import uuid
from pathlib import Path

from memtorture.protocol import ProtocolError, Worker, decode, encode, parse_reply
from memtorture.types import Ack, NotSupported, Record
from memtorture.wire import decode_args

FIXTURES = Path(__file__).resolve().parents[1] / "docs/protocol/v1"


def active_run(directory):
    try:
        pid = int((directory / ".running").read_text())
    except (FileNotFoundError, ValueError):
        return False
    except OSError:
        return True
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except OSError:
        return True
    return True


def prune_failed_runs(root, keep=10):
    if not root.is_dir():
        return
    try:
        directories = list(root.iterdir())
    except FileNotFoundError:
        return
    candidates = []
    for directory in directories:
        if directory.is_symlink() or not directory.is_dir() or active_run(directory):
            continue
        try:
            candidates.append((directory.stat().st_mtime_ns, directory.name, directory))
        except FileNotFoundError:
            continue
    for _, _, directory in sorted(candidates, reverse=True)[keep:]:
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(directory)


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
            line = (
                bytes.fromhex(case["raw_hex"])
                if "raw_hex" in case
                else json.dumps(case["reply"]).encode()
            )
            reply = parse_reply(line, 1, case["op"], decode_args(case["args"]))
            decode(reply.get("result"))
        except (ProtocolError, ValueError, UnicodeError):
            accepted = False
        if accepted != case["valid"]:
            raise ProtocolError(f"Malformed fixture disagrees: {case['name']}")
    return len(fixtures)


def search_matches(rows, args, *, first=None, absent=()):
    if len(rows) > args["k"]:
        raise ProtocolError("search returned more than k records")
    if len({record.id for record in rows}) != len(rows):
        raise ProtocolError("search returned duplicate record ids")
    if any(record.scope != args["scope"] for record in rows):
        raise ProtocolError("search returned a record from another scope")
    if any(not record.valid() for record in rows):
        raise ProtocolError("search returned a torn record")
    if first is not None and first.text != args["query"]:
        raise ProtocolError("Golden search rank checks require exact query text")
    forbidden = {record.id for record in absent}
    if any(record.id in forbidden for record in rows):
        return False
    return first is None or args["k"] == 0 or bool(rows) and rows[0] == first


def run_conformance(command, *, config="default", workdir=None, visibility_timeout=10):
    root = Path(".conformance-work").resolve()
    prune_failed_runs(root)
    argv = shlex.split(command)
    if not argv:
        raise ValueError("--command must contain an executable")
    malformed = check_malformed()
    path = Path(workdir) if workdir else root / uuid.uuid4().hex
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=False)
    marker = path / ".running"
    records, refs, ref_owners = {}, {}, {}
    passed, skipped = 0, []
    succeeded = {"start"}
    success = False

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
        marker.write_text(str(os.getpid()))
        with Worker(
            "conformance", config, path / "data", command=argv, timeout=10
        ) as worker:
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
                    raw_args = expand(request["args"])
                    args = decode_args(raw_args)
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
                    result = worker.call_raw(op, raw_args)
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
                    raise ProtocolError(f"{op} failed: {exc}") from exc
                if "error" in case["reply"]:
                    raise ProtocolError(f"{op} accepted an unreported server PID")
                if isinstance(result, Ack) and op in {"write", "upsert"}:
                    if result.dropped:
                        raise ProtocolError(f"{op} dropped the golden record")
                    if result.ref is not None:
                        record = args["record"]
                        owner = (record.scope.tenant, record.scope.session, record.key)
                        prior = ref_owners.get(result.ref.id, owner)
                        if prior != owner:
                            raise ProtocolError(
                                "Distinct writes returned the same native ref"
                            )
                        ref_owners[result.ref.id] = owner
                        refs[result.id] = result.ref.id
                    if "get_by_ref" in worker.capabilities and result.ref is None:
                        raise ProtocolError("get_by_ref requires native write refs")
                match = case.get("match")
                if op == "search":
                    first = decode(expand(case.get("first")))
                    absent = decode(expand(case.get("absent", [])))
                    deadline = time.monotonic() + visibility_timeout
                    while not search_matches(result, args, first=first, absent=absent):
                        if time.monotonic() >= deadline:
                            raise ProtocolError(
                                f"search membership or rank disagrees with golden case "
                                f"{request['id']}"
                            )
                        time.sleep(0.01)
                        result = worker.call_raw(op, raw_args)
                elif match == "capabilities":
                    if set(result) != worker.capabilities:
                        raise ProtocolError("Capabilities changed after start")
                elif match != "shape" and op != "delete":
                    value = case["reply"]["result"]
                    if isinstance(result, Ack) and result.ref is None and op == "write":
                        value = value | {"ref": None}
                    expected = decode(expand(value))
                    deadline = time.monotonic() + visibility_timeout
                    while (
                        result != expected
                        and op in {"get", "get_by_ref", "read_recent", "list_prefix"}
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                        result = worker.call_raw(op, raw_args)
                    if result != expected:
                        raise ProtocolError(
                            f"{op} disagrees with golden reply {request['id']}"
                        )
                    rows = result if isinstance(result, list) else [result]
                    if any(isinstance(r, Record) and not r.valid() for r in rows):
                        raise ProtocolError(f"{op} returned a torn golden record")
                passed += 1
                succeeded.add(op)
        success = True
        return {"passed": passed, "skipped": skipped, "harness_checks": malformed}
    finally:
        marker.unlink(missing_ok=True)
        if success:
            shutil.rmtree(path)
        else:
            print(f"Work directory kept: {path}")
            try:
                logs = list((path / "logs").glob("*.stderr.log"))
                if not logs:
                    raise FileNotFoundError("No worker stderr log")
                with logs[0].open("rb") as log:
                    log.seek(0, 2)
                    log.seek(max(0, log.tell() - 16384))
                    tail = (
                        log.read().decode("utf-8", errors="replace").splitlines()[-30:]
                    )
                print("Worker stderr tail:\n" + ("\n".join(tail) or "(empty)"))
            except OSError as exc:
                print(f"Worker stderr unavailable: {exc}")
            prune_failed_runs(root)
        if success and workdir is None:
            try:
                path.parent.rmdir()
            except OSError:
                pass
