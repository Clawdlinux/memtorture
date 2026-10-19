import sqlite3

from memtorture.adapters.base import Base
from memtorture.types import Ack, NotSupported, Record, Scope


class SQLite(Base):
    """SQLite WAL and transactionally maintained FTS5. Key maps to path."""

    name = "sqlite"
    version = sqlite3.sqlite_version
    notes = (
        "WAL commit. NORMAL does not sync each commit. FULL syncs the WAL per commit. "
        "https://www.sqlite.org/pragma.html#pragma_synchronous"
    )

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
            "keyed",
        }

    def start(self, workdir, config):
        if config not in {"synchronous=NORMAL", "synchronous=FULL", "synchronous=OFF"}:
            raise NotSupported(f"Unknown SQLite row: {config}")
        self.workdir = workdir
        workdir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(
            workdir / "memory.sqlite", timeout=30, isolation_level=None
        )
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(f"PRAGMA synchronous={config.split('=')[1]}")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS records (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                tenant TEXT NOT NULL,
                session TEXT NOT NULL,
                path TEXT NOT NULL,
                text TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                UNIQUE(tenant, session, path)
            );
            CREATE INDEX IF NOT EXISTS scope_seq ON records(tenant, session, seq);
            CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text);
            CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN
                INSERT INTO fts(rowid, text) VALUES(new.seq, new.text);
            END;
            CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN
                DELETE FROM fts WHERE rowid=old.seq;
            END;
        """)
        self.db.execute("PRAGMA recursive_triggers=ON")

    @staticmethod
    def _record(row):
        return Record(row[0], Scope(row[1], row[2]), row[3], row[4], row[5])

    def _query(self, scope, where="", params=(), limit=1000001, *, ordered=True):
        ordering = " ORDER BY seq DESC" if ordered else ""
        rows = self.db.execute(
            "SELECT id,tenant,session,path,text,payload_sha256 FROM records "
            "WHERE tenant=? AND session=? " + where + ordering + " LIMIT ?",
            (scope.tenant, scope.session, *params, limit),
        )
        return [self._record(row) for row in rows]

    def write(self, scope, record):
        if scope != record.scope:
            raise ValueError("Scope mismatch")
        self.db.execute(
            "INSERT OR REPLACE INTO records"
            "(id,tenant,session,path,text,payload_sha256) VALUES(?,?,?,?,?,?)",
            (
                record.id,
                scope.tenant,
                scope.session,
                record.key,
                record.text,
                record.payload_sha256,
            ),
        )
        return Ack(record.id)

    def get(self, scope, key):
        rows = self._query(scope, "AND path=?", (key,), 1, ordered=False)
        if not rows:
            rows = self._query(scope, "AND id=?", (key,), 1, ordered=False)
        return rows[0] if rows else None

    def bulk_load(self, scope, records):
        self.db.execute("BEGIN")
        try:
            for record in records:
                self.write(scope, record)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def read_recent(self, scope, n):
        return self._query(scope, limit=n)

    def list_prefix(self, scope, prefix, limit):
        if not prefix:
            return self._query(scope, limit=limit)
        return self._query(
            scope,
            "AND path >= ? AND path < ?",
            (prefix, prefix + "\U0010ffff"),
            limit,
            ordered=False,
        )

    def search(self, scope, query, k):
        if not query.strip():
            return []
        escaped = '"' + query.replace('"', '""') + '"'
        return self._query(
            scope,
            "AND seq IN (SELECT rowid FROM fts WHERE fts MATCH ?)",
            (escaped,),
            k,
        )

    def upsert(self, scope, key, value):
        return self.write(scope, Record.from_value(scope, key, value))

    def delete(self, scope, ref):
        self.db.execute(
            "DELETE FROM records WHERE tenant=? AND session=? AND id=?",
            (scope.tenant, scope.session, ref.id),
        )
        return Ack(ref.id)

    def stop(self, target="all", kill=False):
        if hasattr(self, "db"):
            self.db.close()
