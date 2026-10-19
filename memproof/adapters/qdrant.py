import contextlib
import fcntl
import hashlib
import json
import os
import signal
import socket
import subprocess
import time
import uuid
from pathlib import Path

from qdrant_client import QdrantClient, models

from memproof.adapters.base import Base
from memproof.embed import DIMENSIONS, embed
from memproof.types import Ack, NotSupported, Record, Ref, Scope

VERSION = "1.16.3"
ROOT = Path(__file__).resolve().parents[2]
COLLECTION = "memory"


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
            if not self.client.collection_exists(COLLECTION):
                self.client.create_collection(
                    COLLECTION,
                    vectors_config=models.VectorParams(
                        size=DIMENSIONS, distance=models.Distance.COSINE
                    ),
                )
                for field in ("tenant", "session", "key", "id"):
                    self.client.create_payload_index(
                        COLLECTION, field, models.PayloadSchemaType.KEYWORD, wait=True
                    )
                self.client.create_payload_index(
                    COLLECTION, "_order", models.PayloadSchemaType.INTEGER, wait=True
                )

    def _connect(self, state):
        client = QdrantClient(
            url=f"http://127.0.0.1:{state['port']}",
            api_key=state["api_key"],
            timeout=60,
            check_compatibility=False,
        )
        try:
            response = client.info()
            if response.version != VERSION:
                raise NotSupported(f"Expected Qdrant {VERSION}, got {response.version}")
        except Exception as exc:
            client.close()
            if isinstance(exc, NotSupported):
                raise
            return False
        self.client = client
        return True

    @staticmethod
    def _filter(scope, **fields):
        fields = {"tenant": scope.tenant, "session": scope.session, **fields}
        return models.Filter(
            must=[
                models.FieldCondition(key=k, match=models.MatchValue(value=v))
                for k, v in fields.items()
            ]
        )

    @staticmethod
    def _record(point):
        data = point.payload
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
        return models.PointStruct(
            id=point_id(scope, record.key),
            vector=embed(record.text),
            payload={
                "id": record.id,
                "tenant": scope.tenant,
                "session": scope.session,
                "key": record.key,
                "text": record.text,
                "payload_sha256": record.payload_sha256,
                "_order": order,
            },
        )

    def write(self, scope, record):
        if record.scope != scope:
            raise ValueError("Scope mismatch")
        self.client.upsert(COLLECTION, [self._point(record)], wait=self.wait)
        return Ack(record.id, ref=Ref(point_id(scope, record.key)))

    def bulk_load(self, scope, records):
        if any(r.scope != scope for r in records):
            raise ValueError("Scope mismatch")
        self.client.upsert(COLLECTION, [self._point(r) for r in records], wait=True)

    def get(self, scope, key):
        points = self.client.retrieve(
            COLLECTION, [point_id(scope, key)], with_payload=True
        )
        if points:
            return self._record(points[0])
        points, _ = self.client.scroll(
            COLLECTION,
            scroll_filter=self._filter(scope, id=key),
            limit=1,
            with_payload=True,
        )
        return self._record(points[0]) if points else None

    def get_by_id(self, scope, ref):
        points = self.client.retrieve(COLLECTION, [ref.id], with_payload=True)
        if not points:
            return None
        record = self._record(points[0])
        return record if record.scope == scope else None

    def read_recent(self, scope, n):
        points, _ = self.client.scroll(
            COLLECTION,
            scroll_filter=self._filter(scope),
            limit=n,
            order_by=models.OrderBy(key="_order", direction=models.Direction.DESC),
            with_payload=True,
        )
        return [self._record(p) for p in points]

    def list_prefix(self, scope, prefix, limit):
        rows, offset = [], None
        while len(rows) < limit:
            points, offset = self.client.scroll(
                COLLECTION,
                scroll_filter=self._filter(scope),
                limit=1000,
                offset=offset,
                with_payload=True,
            )
            rows.extend(
                self._record(p) for p in points if p.payload["key"].startswith(prefix)
            )
            if offset is None:
                break
        return rows[:limit]

    def search(self, scope, query, k):
        response = self.client.query_points(
            COLLECTION,
            query=embed(query),
            query_filter=self._filter(scope),
            limit=k,
            with_payload=True,
        )
        return [self._record(p) for p in response.points]

    def upsert(self, scope, key, value):
        return self.write(scope, Record.from_value(scope, key, value))

    def delete(self, scope, ref):
        self.client.delete(
            COLLECTION,
            points_selector=models.FilterSelector(
                filter=self._filter(scope, id=ref.id)
            ),
            wait=self.wait,
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
