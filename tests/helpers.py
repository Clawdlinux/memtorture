import contextlib
import gzip
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from memtorture.adapters.markdown import Markdown
from memtorture.protocol import Worker
from memtorture.runner import ROWS
from memtorture.types import Ack, Record, Ref, Scope

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
REFERENCE = ROOT / "systems/reference-node/worker.mjs"
LOCAL_ROWS = [
    ("markdown", "append"),
    ("markdown", "rewrite"),
    ("sqlite", "synchronous=NORMAL"),
    ("sqlite", "synchronous=FULL"),
    *(
        (name, "default")
        for name in (
            "md-rmw-sleep",
            "buffered",
            "sqlite-off",
            "sqlite-busy",
            "sqlite-visible",
            "delayed-index",
        )
    ),
]
ADAPTER_ROWS = sorted(
    set(LOCAL_ROWS)
    | {(name, config) for name, configs in ROWS.items() for config in configs}
)
KEYWORD_SEARCH = (
    "return selected.reverse().filter(row => row.text.includes(args.query))"
    ".slice(0, args.k);"
)
NEIGHBOUR_SEARCH = (
    "return selected.sort((a, b) => "
    "Number(b.text === args.query) - Number(a.text === args.query)).slice(0, args.k);"
)
SELECTED = (
    "const selected = [...rows.values()]"
    ".filter(row => sameScope(row.scope, args.scope));"
)


def replace_source(source, old, new):
    assert old in source, f"Worker source no longer contains {old!r}"
    return source.replace(old, new)


def broken_source(bug):
    code = REFERENCE.read_text()
    if bug == "stale-upsert-search":
        replacements = [
            (
                "let rows = new Map();",
                "let rows = new Map(); const stale = new Map();",
            ),
            (
                "rows.delete(ref);",
                "{ stale.set(ref, row); rows.delete(ref); }",
            ),
            (
                KEYWORD_SEARCH,
                "return selected.reverse().filter(row => row.text.includes(args.query))"
                ".concat([...stale.values()].filter(row => "
                "sameScope(row.scope, args.scope)))"
                ".slice(0, args.k);",
            ),
        ]
    elif bug == "duplicate-search":
        replacements = [
            (
                "let rows = new Map();",
                "let rows = new Map(); const saved = new Map();",
            ),
            (
                "const value = record(args.record, args.scope);",
                "const value = record(args.record, args.scope); "
                "saved.set(value.id, value);",
            ),
            (
                KEYWORD_SEARCH,
                "const hits = selected.reverse()"
                ".filter(row => row.text.includes(args.query));"
                "const other = [...saved.values()].find(row => "
                "hits.length && row.id !== hits[0].id "
                "&& sameScope(row.scope, args.scope));"
                "if (args.k >= 3 && hits.length && other) "
                "return [hits[0], hits[0], other];"
                "return hits.slice(0, args.k);",
            ),
        ]
    elif bug.startswith("scope:"):
        op = bug.split(":")[1]
        replacements = [
            (
                SELECTED,
                "const selected = [...rows.values()].filter(row => "
                f'op === "{op}" || sameScope(row.scope, args.scope));',
            )
        ]
    elif bug.startswith("deleted:"):
        op = bug.split(":")[1]
        replacements = [
            (
                "let rows = new Map();",
                "let rows = new Map(); const deletedRows = new Map();",
            ),
            (
                "if (visible) rows.delete(args.ref.id);",
                "if (visible) { deletedRows.set(args.ref.id, found); "
                "rows.delete(args.ref.id); }",
            ),
            (
                SELECTED,
                "const selected = [...rows.values(), "
                f'...(op === "{op}" ? deletedRows.values() : [])]'
                ".filter(row => sameScope(row.scope, args.scope));",
            ),
        ]
    elif bug == "duplicate-upsert":
        replacements = [("if (replace) {", "if (false) {")]
    elif bug == "constant-ref":
        replacements = [
            (
                'return ack(value.id, { kind: "ref", id: ref });',
                'return ack(value.id, { kind: "ref", id: "constant" });',
            ),
            (
                "const found = rows.get(args.ref.id);",
                "const found = [...rows.values()].reverse()"
                '.find(row => row.key === "path/one");',
            ),
            (
                "if (visible) rows.delete(args.ref.id);",
                "if (visible) { for (const [id, row] of rows) "
                "{ if (row.id === found.id) rows.delete(id); } }",
            ),
        ]
    else:
        assert bug == "reject-unknown-scope"
        replacements = [
            (
                "scope(args.scope);",
                'scope(args.scope); check(!("future" in args.scope), '
                '"Unknown scope field");',
            )
        ]
    for old, new in replacements:
        code = replace_source(code, old, new)
    return code


def variant_source(variant):
    if variant in {"echo-delete-ref", "dropped-foreign-delete"}:
        replacement = (
            "return ack(args.ref.id, args.ref);"
            if variant == "echo-delete-ref"
            else "return { ...ack(args.ref.id), dropped: !visible };"
        )
        return replace_source(
            REFERENCE.read_text(), "return ack(args.ref.id);", replacement
        )
    return broken_source(variant)


def make_record(key="path/file", text="needle0001 text", scope=None, **kwargs):
    return Record.make(scope or Scope("tenant", "session"), key, text, **kwargs)


def probe_command(name="Probe", *, nonzero=False):
    return [
        sys.executable,
        "-c",
        f"from tests.helpers import {name}; "
        f"from memtorture.worker import serve; serve({name}())"
        + ("; import sys; sys.exit(7)" if nonzero else ""),
    ]


class Probe:
    name = "probe"

    def start(self, workdir, config):
        self.records = {}
        self.config = config

    def capabilities(self):
        return {"write", "get"} | (
            {"get_by_ref"} if self.config != "no-capability" else set()
        )

    def write(self, scope, record):
        self.records[record.key] = record
        return Ack(
            record.id,
            dropped=self.config == "dropped",
            ref=None if self.config == "no-ref" else Ref("native-point-id"),
        )

    def get(self, scope, key):
        return self.records.get(key)

    def get_by_ref(self, scope, ref):
        return next(iter(self.records.values()), None)

    def stop(self, target="all", kill=False):
        pass


class ServerProbe(Probe):
    def start(self, workdir, config):
        super().start(workdir, config)
        self.children = [
            subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for _ in range(2)
        ]
        self.servers = [
            {"role": role, "pid": child.pid}
            for role, child in zip(("storage", "api"), self.children, strict=True)
        ]

    def stop(self, target="all", kill=False):
        if self.config == "timeout":
            time.sleep(60)
        if self.config == "error":
            raise RuntimeError("stop failed")
        if self.config == "malformed":
            return {}
        if self.config == "nonzero":
            return None
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)


def reply_command(raw, *, stop=False, trailing=b""):
    start = (
        '{"id":1,"result":{"protocol":1,"capabilities":["write"],'
        '"version":"test","notes":"","servers":[]}}'
    )
    return [
        sys.executable,
        "-c",
        (
            "import sys,time; sys.stdin.readline(); "
            f"print({start!r},flush=True); sys.stdin.readline(); "
            f"sys.stdout.buffer.write({raw!r}); sys.stdout.buffer.flush(); "
            + (
                f"time.sleep(.03); sys.stdout.buffer.write({trailing!r}); "
                "sys.stdout.buffer.flush(); sys.stdin.read()"
                if stop
                else "sys.stdin.read()"
            )
        ),
    ]


def pid_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def cleanup_worker(worker):
    pids = list(worker.servers.values())
    if worker.process.poll() is None:
        worker.kill("worker")
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while any(pid_exists(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.close()


class HistoryMarkdown(Markdown):
    def capabilities(self):
        return super().capabilities() | {"history"}

    def upsert(self, scope, record):
        return self.write(scope, record)


class HistoryWorker(Worker):
    def __init__(self, system, config, workdir, **kwargs):
        super().__init__(
            system,
            config,
            workdir,
            command=probe_command("HistoryMarkdown"),
            cwd=ROOT,
            **kwargs,
        )


class ProbeWorker(Worker):
    def __init__(self, system, config, workdir):
        super().__init__(system, config, workdir, command=probe_command(), cwd=ROOT)


class ReadbackWorker(Worker):
    fault = ""

    def call(self, op, **kwargs):
        if op == "list_prefix" and self.fault == "torn":
            with (self.workdir / "memory.md").open("a") as output:
                output.write("torn first\n{torn second\n")
            self.fault = ""
        rows = super().call(op, **kwargs)
        if op == "list_prefix" and kwargs["prefix"] == "shared-":
            if self.fault == "duplicate":
                return rows + [rows[0], make_record("extra", "unknown", rows[0].scope)]
            if self.fault.startswith("corrupt"):
                bad = replace(rows[0], text="corrupt")
                return [bad, *rows] if self.fault == "corrupt-first" else [*rows, bad]
        return rows


class TimingWorker(Worker):
    instances = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.faulted = False
        self.instances.append(self)

    def kill(self, target):
        super().kill(target)
        self.faulted = True


class QdrantClient:
    def __init__(self, point):
        self.point = point
        self.calls = []
        self.visible = True

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST" and path.endswith("/points"):
            found = self.visible and self.point["id"] in body["ids"]
            return {"result": [{"payload": self.point["payload"]}] if found else []}
        return {"result": {"status": "acknowledged"}}


class GitProvenance:
    def __init__(self, directory, source_dirty):
        self.directory = directory
        self.source_dirty = source_dirty
        self.status_commands = []

    def __call__(self, command, **kwargs):
        assert not self.directory.exists(), "Capture provenance before creating output"
        if command == ["git", "rev-parse", "HEAD"]:
            return "a" * 40 + "\n"
        assert command[:3] == ["git", "status", "--porcelain"]
        self.status_commands.append(command)
        if self.source_dirty:
            return " M memtorture/embed.py\n"
        if ":(top,exclude)results/controls" in command:
            return ""
        return " M results/controls/full/operations.jsonl\n"


class LastRowWorker:
    capabilities = {"keyed", "upsert", "get"}

    def __init__(self):
        self.last = None

    def call(self, op, **kwargs):
        if op == "upsert":
            self.last = kwargs["record"]
            return Ack(self.last.id)
        if op == "get":
            return self.last
        raise NotImplementedError(op)


def stub_control_worker(system, *args, **kwargs):
    return contextlib.nullcontext(SimpleNamespace(system=system, info={}))


def stub_supersede(worker, **kwargs):
    return {"status": "violation" if worker.system == "append-oldest" else "ok"}


def stub_recall(system, config, path, *, paths, **kwargs):
    return {
        "paths": {
            paths[0]: {
                "observations": [
                    {
                        "lag_ms": 100.5 if system == "delayed-index" else 0.1,
                        "polls": 1,
                    }
                ],
                "read_ms": {"p99": 0.1},
            }
        }
    }


def stub_kill(system, *args, **kwargs):
    return {"status": "violation" if system == "buffered" else "ok"}


def stub_concurrent(suite, system, *args, **kwargs):
    return {"status": "violation" if system == "md-rmw-sleep" else "ok"}


SCHEMA_KEYWORDS = {
    "$schema",
    "$defs",
    "$ref",
    "type",
    "const",
    "enum",
    "required",
    "properties",
    "items",
    "anyOf",
    "oneOf",
    "allOf",
    "not",
    "minimum",
    "minItems",
    "maxItems",
    "minLength",
}


def schema_equal(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


def matches(schema, value, path):
    """Evaluate the JSON Schema keywords used by the public wire schemas."""
    assert not schema.keys() - SCHEMA_KEYWORDS, "Unsupported schema keyword"
    if "$ref" in schema:
        filename, pointer = schema["$ref"].split("#", 1)
        target_path = path.parent / filename if filename else path
        target = json.loads(target_path.read_text())
        for part in pointer.lstrip("/").split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        if not matches(target, value, target_path):
            return False
    for keyword, predicate in (
        ("anyOf", any),
        ("allOf", all),
        ("oneOf", lambda results: sum(results) == 1),
    ):
        if keyword in schema and not predicate(
            matches(child, value, path) for child in schema[keyword]
        ):
            return False
    if "not" in schema and matches(schema["not"], value, path):
        return False
    number = type(value) in (int, float) and math.isfinite(value)
    types = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": type(value) is bool,
        "null": value is None,
        "number": number,
        "integer": number and int(value) == value,
    }
    if "type" in schema and not types[schema["type"]]:
        return False
    if "const" in schema and not schema_equal(value, schema["const"]):
        return False
    if "enum" in schema and not any(
        schema_equal(value, item) for item in schema["enum"]
    ):
        return False
    if "minimum" in schema and number and value < schema["minimum"]:
        return False
    if isinstance(value, dict):
        if not set(schema.get("required", ())) <= value.keys():
            return False
        if not all(
            matches(child, value[key], path)
            for key, child in schema.get("properties", {}).items()
            if key in value
        ):
            return False
    if isinstance(value, list):
        if (
            not schema.get("minItems", 0)
            <= len(value)
            <= schema.get("maxItems", math.inf)
        ):
            return False
        if "items" in schema and not all(
            matches(schema["items"], v, path) for v in value
        ):
            return False
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        return False
    return True


class EncodedMemory(Markdown):
    name = "encoded-memory-test"

    def start(self, workdir, config):
        super().start(workdir, "append")
        self.mode = config
        self.path = workdir / "memory.gz"
        existed = self.path.exists()
        if not existed:
            self.path.write_bytes(gzip.compress(b""))
        elif config == "recovery-loss":
            self._rewrite(lambda rows: [r for r in rows if r.key != "key00000000"])

    def capabilities(self):
        missing = {
            "no-upsert": {"upsert"},
            "no-get": {"get"},
            "no-delete": {"delete"},
            "no-list": {"list_prefix"},
        }.get(self.mode, set())
        return super().capabilities() - missing

    def _all(self):
        records = []
        for line in gzip.decompress(self.path.read_bytes()).decode().splitlines():
            data = json.loads(line)
            data["scope"] = Scope(**data["scope"])
            records.append(Record(**data))
        return records

    def _rewrite(self, transform):
        records = transform(self._all())
        self.path.write_bytes(
            gzip.compress("".join(r.value() + "\n" for r in records).encode())
        )

    def write(self, scope, record):
        self._rewrite(lambda rows: rows + [record])
        return Ack(record.id, ref=None if self.mode == "no-ref" else Ref(record.id))

    def bulk_load(self, scope, records):
        self._rewrite(lambda rows: rows + records)

    def search(self, scope, query, k, vector=None):
        if self.mode == "unreadable-search":
            return []
        return super().search(scope, query, k, vector)


def stub_forget(system, *args, **kwargs):
    return {
        "status": "violation"
        if system in {"sqlite-forget-fts", "sqlite-residue-off"}
        else "ok"
    }
