import json
import os
import time
import uuid

from memproof.adapters.base import Base
from memproof.types import Ack, NotSupported, Record, Scope


class Markdown(Base):
    """Flat Markdown list. Reads scan rows. Rewrites have no writer lock."""

    name = "markdown"
    version = "stdlib-jsonl-v1"
    notes = (
        "No documented durable mode. Substring search and keyed rewrites scan files. "
        "Append uses one unbuffered O_APPEND write. No fsync before ack."
    )
    rmw_sleep = 0

    def capabilities(self):
        return {
            "write",
            "get",
            "get-id",
            "read_recent",
            "list_prefix",
            "search",
            "upsert",
            "delete",
            "emulated:get",
            "emulated:list_prefix",
            "emulated:read_recent",
            "emulated:upsert",
            "emulated:delete",
            "keyed",
        }

    def start(self, workdir, config):
        if config not in {"append", "rewrite"}:
            raise NotSupported(f"Unknown Markdown row: {config}")
        self.config, self.workdir = config, workdir
        workdir.mkdir(parents=True, exist_ok=True)
        self.path = workdir / "memory.md"
        self.path.touch(exist_ok=True)

    def _all(self):
        rows = []
        for line in self.path.read_text().splitlines():
            if not line:
                continue
            try:
                data = json.loads(line.removeprefix("- "))
                data["scope"] = Scope(**data["scope"])
                rows.append(Record(**data))
            except (ValueError, TypeError, KeyError):
                # Preserve malformed rows as torn evidence, never silently drop them.
                rows.append(Record("malformed", Scope("", ""), "", line, "invalid"))
        return rows

    def _rows(self, scope):
        return [r for r in self._all() if r.scope == scope or not r.valid()]

    def _rewrite(self, transform):
        rows = self._all()
        if self.rmw_sleep:
            time.sleep(self.rmw_sleep)
        rows = transform(rows)
        staging = self.workdir / f".rewrite-{uuid.uuid4().hex}"
        staging.write_text("".join(f"- {r.value()}\n" for r in rows))
        os.replace(staging, self.path)

    def write(self, scope, record):
        if record.scope != scope:
            raise ValueError("Scope mismatch")
        if self.config == "rewrite":
            self._rewrite(lambda rows: rows + [record])
        else:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND)
            try:
                data = f"- {record.value()}\n".encode()
                if os.write(fd, data) != len(data):
                    raise OSError("Short append")
            finally:
                os.close(fd)
        return Ack(record.id)

    def bulk_load(self, scope, records):
        if any(r.scope != scope for r in records):
            raise ValueError("Scope mismatch")
        self._rewrite(lambda rows: rows + records)

    def read_recent(self, scope, n):
        return list(reversed(self._rows(scope)))[:n]

    def get(self, scope, key):
        return next(
            (r for r in reversed(self._rows(scope)) if key in (r.key, r.id)), None
        )

    def list_prefix(self, scope, prefix, limit):
        return [r for r in self._rows(scope) if r.key.startswith(prefix)][:limit]

    def search(self, scope, query, k):
        return [r for r in reversed(self._rows(scope)) if query in r.text][:k]

    def upsert(self, scope, key, value):
        record = Record.from_value(scope, key, value)
        self._rewrite(
            lambda rows: (
                [r for r in rows if (r.scope, r.key) != (scope, key)] + [record]
            )
        )
        return Ack(record.id)

    def delete(self, scope, ref):
        self._rewrite(
            lambda rows: [r for r in rows if (r.scope, r.id) != (scope, ref.id)]
        )
        return Ack(ref.id)
