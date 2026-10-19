import contextlib
import fcntl
import hashlib
import http.client
import json
import os
import signal
import socket
import subprocess
import time
import uuid
from pathlib import Path

from memproof.adapters.base import Base
from memproof.embed import DIMENSIONS, embed
from memproof.types import Ack, NotSupported, Record, Ref, Scope

VERSION = "1.16.3"
ROOT = Path(__file__).resolve().parents[2]
COLLECTION = "memory"


class Rest:
    """Qdrant REST over one keep-alive connection, so no request pays a TCP connect."""

    def __init__(self, port, api_key, timeout=60):
        self.port, self.api_key, self.timeout = port, api_key, timeout
        self.conn = None

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        headers = {"api-key": self.api_key, "content-type": "application/json"}
        for attempt in (0, 1):
            if self.conn is None:
                self.conn = http.client.HTTPConnection(
                    "127.0.0.1", self.port, timeout=self.timeout
                )
            try:
                self.conn.request(method, path, data, headers)
                response = self.conn.getresponse()
                payload = response.read()
            except (http.client.RemoteDisconnected, ConnectionError):
                # Stale keep-alive socket. Calls are idempotent, so one retry is safe.
                self.close()
                if attempt:
                    raise
                continue
            if response.status >= 300:
                raise RuntimeError(
                    f"Qdrant {method} {path}: {response.status} {payload[:200]!r}"
                )
            return json.loads(payload)
        raise AssertionError("unreachable")

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None


def _wait(flag):
    return "true" if flag else "false"


def point_id(scope, key):
    value = json.dumps([scope.tenant, scope.session, key])
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


class Qdrant(Base):
    """wait controls when writes are applied and visible, not fsync.

    Upstream: "If true, wait for changes to actually happen."
    https://api.qdrant.tech/v-1-16-x/api-reference/points/upsert-points
    """

    name = "qdrant"
    version = VERSION
    notes = (
        "wait controls applied visibility, not fsync. "
        "No documented fsync-before-ack row. "
        "https://api.qdrant.tech/v-1-16-x/api-reference/points/upsert-points "
        "Scope filters and recency ordering are native. Prefix matching is local."
    )

    def capabilities(self):
        return {
            "write",
            "get",
            "get-id",
            "get_by_id",
            "read_recent",
            "list_prefix",
            "search",
            "upsert",
            "delete",
            "keyed",
            "emulated:list_prefix",
        }

    def start(self, workdir, config):
        if config not in {"wait=false", "wait=true"}:
            raise NotSupported(f"Unknown Qdrant row: {config}")
        tool = ROOT / ".tools/qdrant" / VERSION
        binary = tool / "qdrant"
        if not binary.is_file():
            raise NotSupported(
                "Pinned Qdrant binary missing. Run scripts.fetch_qdrant."
            )
        expected = (tool / "binary.sha256").read_text().strip()
        if hashlib.sha256(binary.read_bytes()).hexdigest() != expected:
            raise NotSupported("Qdrant binary changed after archive verification")
        self.runtime_versions = {"qdrant": VERSION, "binary_sha256": expected}
        self.workdir, self.wait = workdir, config == "wait=true"
        workdir.mkdir(parents=True, exist_ok=True)
        self.owner = None
        self.log = None
        with (workdir / "server.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            descriptor = workdir / "server.json"
            state = json.loads(descriptor.read_text()) if descriptor.exists() else None
            if state and not self._connect(state):
                state = None
            if not state:
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    port = reservation.getsockname()[1]
                api_key = uuid.uuid4().hex
                settings = {
                    "log_level": "WARN",
                    "telemetry_disabled": True,
                    "storage": {
                        "storage_path": str((workdir / "storage").resolve()),
                        "snapshots_path": str((workdir / "snapshots").resolve()),
                    },
                    "service": {
                        "host": "127.0.0.1",
                        "http_port": port,
                        "grpc_port": None,
                        "api_key": api_key,
                        "enable_cors": False,
                    },
                }
                config_path = workdir / "qdrant.yaml"
                config_path.write_text(json.dumps(settings))
                config_path.chmod(0o600)
                self.log = (workdir / "qdrant.stderr.log").open("ab")
                self.owner = subprocess.Popen(
                    [
                        str(binary),
                        "--config-path",
                        str(config_path.resolve()),
                        "--disable-telemetry",
                    ],
                    cwd=workdir,
                    stdout=self.log,
                    stderr=self.log,
                    env=os.environ
                    | {
                        "QDRANT__SERVICE__HOST": "127.0.0.1",
                        "QDRANT__SERVICE__HTTP_PORT": str(port),
                        "QDRANT__SERVICE__API_KEY": api_key,
                        "QDRANT__TELEMETRY_DISABLED": "true",
                    },
                )
                state = {"pid": self.owner.pid, "port": port, "api_key": api_key}
                deadline = time.monotonic() + 30
                while not self._connect(state):
                    if self.owner.poll() is not None or time.monotonic() >= deadline:
                        self.stop()
                        raise RuntimeError(f"Qdrant failed to start. See {workdir}")
                    time.sleep(0.02)
                descriptor.write_text(json.dumps(state))
                descriptor.chmod(0o600)
            self.server_pid = state["pid"]
            base = f"/collections/{COLLECTION}"
            if not self.client.call("GET", f"{base}/exists")["result"]["exists"]:
                self.client.call(
                    "PUT",
                    base,
                    {"vectors": {"size": DIMENSIONS, "distance": "Cosine"}},
                )
                schemas = [(f, "keyword") for f in ("tenant", "session", "key", "id")]
                for field, schema in [*schemas, ("_order", "integer")]:
                    self.client.call(
                        "PUT",
                        f"{base}/index?wait=true",
                        {"field_name": field, "field_schema": schema},
                    )

    def _connect(self, state):
        client = Rest(state["port"], state["api_key"])
        try:
            found = client.call("GET", "/")["version"]
        except (OSError, RuntimeError, ValueError):
            client.close()
            return False
        if found != VERSION:
            client.close()
            raise NotSupported(f"Expected Qdrant {VERSION}, got {found}")
        self.client = client
        return True

    def _points(self, op, body, wait=None, method="POST"):
        query = "" if wait is None else f"?wait={_wait(wait)}"
        path = f"/collections/{COLLECTION}/points{op}{query}"
        return self.client.call(method, path, body)["result"]

    @staticmethod
    def _filter(scope, **fields):
        fields = {"tenant": scope.tenant, "session": scope.session, **fields}
        return {"must": [{"key": k, "match": {"value": v}} for k, v in fields.items()]}

    @staticmethod
    def _record(point):
        data = point["payload"]
        return Record(
            data["id"],
            Scope(data["tenant"], data["session"]),
            data["key"],
            data["text"],
            data["payload_sha256"],
        )

    @staticmethod
    def _point(record):
        scope = record.scope
        order = getattr(record, "sequence", None)
        if order is None:
            # Without a harness sequence, wall time orders independent worker processes.
            order = time.time_ns()
        return {
            "id": point_id(scope, record.key),
            "vector": embed(record.text),
            "payload": {
                "id": record.id,
                "tenant": scope.tenant,
                "session": scope.session,
                "key": record.key,
                "text": record.text,
                "payload_sha256": record.payload_sha256,
                "_order": order,
            },
        }

    def write(self, scope, record):
        if record.scope != scope:
            raise ValueError("Scope mismatch")
        self._points("", {"points": [self._point(record)]}, self.wait, "PUT")
        return Ack(record.id, ref=Ref(point_id(scope, record.key)))

    def bulk_load(self, scope, records):
        if any(r.scope != scope for r in records):
            raise ValueError("Scope mismatch")
        self._points("", {"points": [self._point(r) for r in records]}, True, "PUT")

    def get(self, scope, key):
        points = self._retrieve(point_id(scope, key))
        if points:
            return self._record(points[0])
        body = {"filter": self._filter(scope, id=key), "limit": 1, "with_payload": True}
        points = self._points("/scroll", body)["points"]
        return self._record(points[0]) if points else None

    def _retrieve(self, pid):
        return self._points("", {"ids": [pid], "with_payload": True})

    def get_by_id(self, scope, ref):
        points = self._retrieve(ref.id)
        if not points:
            return None
        record = self._record(points[0])
        return record if record.scope == scope else None

    def read_recent(self, scope, n):
        body = {
            "filter": self._filter(scope),
            "limit": n,
            "order_by": {"key": "_order", "direction": "desc"},
            "with_payload": True,
        }
        return [self._record(p) for p in self._points("/scroll", body)["points"]]

    def list_prefix(self, scope, prefix, limit):
        rows, offset = [], None
        while len(rows) < limit:
            body = {"filter": self._filter(scope), "limit": 1000, "with_payload": True}
            if offset is not None:
                body["offset"] = offset
            page = self._points("/scroll", body)
            points, offset = page["points"], page.get("next_page_offset")
            rows.extend(
                self._record(p)
                for p in points
                if p["payload"]["key"].startswith(prefix)
            )
            if offset is None:
                break
        return rows[:limit]

    def search(self, scope, query, k):
        body = {
            "query": embed(query),
            "filter": self._filter(scope),
            "limit": k,
            "with_payload": True,
        }
        return [self._record(p) for p in self._points("/query", body)["points"]]

    def upsert(self, scope, key, value):
        return self.write(scope, Record.from_value(scope, key, value))

    def delete(self, scope, ref):
        self._points(
            "/delete", {"filter": self._filter(scope, id=ref.id)}, wait=self.wait
        )
        return Ack(ref.id)

    def stop(self, target="all", kill=False):
        if hasattr(self, "client"):
            self.client.close()
        pid = self.owner.pid if self.owner else self.server_pid
        if pid:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL if kill else signal.SIGTERM)
            if self.owner:
                try:
                    self.owner.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.kill(pid, signal.SIGKILL)
                    self.owner.wait(timeout=10)
        if self.log:
            self.log.close()
