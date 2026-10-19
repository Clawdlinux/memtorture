import contextlib
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


def broken_source(bug):
    code = REFERENCE.read_text()
    keyword_search = (
        "return selected.reverse().filter(row => row.text.includes(args.query))"
        ".slice(0, args.k);"
    )
    if bug == "stale-upsert-search":
        assert keyword_search in code
        return (
            code.replace(
                "let rows = new Map();",
                "let rows = new Map(); const stale = new Map();",
            )
            .replace(
                "rows.delete(ref);",
                "{ stale.set(ref, row); rows.delete(ref); }",
            )
            .replace(
                keyword_search,
                "return selected.reverse().filter(row => row.text.includes(args.query))"
                ".concat([...stale.values()].filter(row => "
                "sameScope(row.scope, args.scope)))"
                ".slice(0, args.k);",
            )
        )
    if bug == "duplicate-search":
        assert keyword_search in code
        return (
            code.replace(
                "let rows = new Map();",
                "let rows = new Map(); const saved = new Map();",
            )
            .replace(
                "const value = record(args.record, args.scope);",
                "const value = record(args.record, args.scope); "
                "saved.set(value.id, value);",
            )
            .replace(
                keyword_search,
                "const hits = selected.reverse()"
                ".filter(row => row.text.includes(args.query));"
                "const other = [...saved.values()].find(row => "
                "hits.length && row.id !== hits[0].id "
                "&& sameScope(row.scope, args.scope));"
                "if (args.k >= 3 && hits.length && other) "
                "return [hits[0], hits[0], other];"
                "return hits.slice(0, args.k);",
            )
        )
    if bug.startswith("scope:"):
        op = bug.split(":")[1]
        return code.replace(
            SELECTED,
            "const selected = [...rows.values()].filter(row => "
            f'op === "{op}" || sameScope(row.scope, args.scope));',
        )
    if bug.startswith("deleted:"):
        op = bug.split(":")[1]
        code = code.replace(
            "let rows = new Map();",
            "let rows = new Map(); const deletedRows = new Map();",
        ).replace(
            "if (visible) rows.delete(args.ref.id);",
            "if (visible) { deletedRows.set(args.ref.id, found); "
            "rows.delete(args.ref.id); }",
        )
        return code.replace(
            SELECTED,
            "const selected = [...rows.values(), "
            f'...(op === "{op}" ? deletedRows.values() : [])]'
            ".filter(row => sameScope(row.scope, args.scope));",
        )
    if bug == "duplicate-upsert":
        return code.replace("if (replace) {", "if (false) {")
    if bug == "constant-ref":
        return (
            code.replace(
                'return ack(value.id, { kind: "ref", id: ref });',
                'return ack(value.id, { kind: "ref", id: "constant" });',
            )
            .replace(
                "const found = rows.get(args.ref.id);",
                "const found = [...rows.values()].reverse()"
                '.find(row => row.key === "path/one");',
            )
            .replace(
                "if (visible) rows.delete(args.ref.id);",
                "if (visible) { for (const [id, row] of rows) "
                "{ if (row.id === found.id) rows.delete(id); } }",
            )
        )
    assert bug == "reject-unknown-scope"
    return code.replace(
        "scope(args.scope);",
        'scope(args.scope); check(!("future" in args.scope), "Unknown scope field");',
    )


def variant_source(variant):
    if variant in {"echo-delete-ref", "dropped-foreign-delete"}:
        replacement = (
            "return ack(args.ref.id, args.ref);"
            if variant == "echo-delete-ref"
            else "return { ...ack(args.ref.id), dropped: !visible };"
        )
        return REFERENCE.read_text().replace("return ack(args.ref.id);", replacement)
    return broken_source(variant).replace(
        "function dispatch(op, args) {",
        'function dispatch(op, args) { console.error("debug diagnostic", op);',
    )


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
