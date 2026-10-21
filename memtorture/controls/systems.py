import threading
import time

from memtorture.adapters.markdown import Markdown
from memtorture.adapters.sqlite import SQLite
from memtorture.types import Ack, Ref


class Buffered(Markdown):
    name = "buffered"
    notes = "Ack before user-space flush. Flush interval 100 ms. No fsync."

    def start(self, workdir, config):
        super().start(workdir, "append")
        self.pending = []
        self.lock = threading.Lock()
        self.finished = threading.Event()
        self.flusher = threading.Thread(target=self._run, daemon=True)
        self.flusher.start()

    def _flush(self):
        with self.lock:
            rows, self.pending = self.pending, []
            for scope, record in rows:
                super().write(scope, record)

    def _run(self):
        while not self.finished.wait(0.1):
            self._flush()

    def write(self, scope, record):
        with self.lock:
            self.pending.append((scope, record))
        return Ack(record.id, ref=Ref(record.id))

    def stop(self, target="all", kill=False):
        self.finished.set()
        self.flusher.join()
        self._flush()


class RmwSleep(Markdown):
    name = "md-rmw-sleep"
    rmw_sleep = 0.005
    notes = "Read-modify-write. Fixed 5 ms sleep inside the rewrite window."

    def start(self, workdir, config):
        super().start(workdir, "rewrite")


class SQLiteOff(SQLite):
    name = "sqlite-off"
    notes = "WAL synchronous=OFF. Commit before ack. Page cache survives SIGKILL."

    def start(self, workdir, config):
        super().start(workdir, "synchronous=OFF")


class SQLiteBusy(SQLite):
    name = "sqlite-busy"
    notes = "WAL synchronous=NORMAL. Busy timeout 30 seconds."

    def start(self, workdir, config):
        super().start(workdir, "synchronous=NORMAL")


class SQLiteVisible(SQLiteBusy):
    name = "sqlite-visible"
    notes = "SQLite is visible on commit."


class DelayedIndex(SQLiteBusy):
    name = "delayed-index"
    notes = "Fixed 100 ms asynchronous delay on every read path."

    def start(self, workdir, config):
        super().start(workdir, config)
        self.visible_at = {}

    def write(self, scope, record):
        ack = super().write(scope, record)
        # A logical deadline isolates visibility delay from OS timer coalescing.
        self.visible_at[record.id] = time.monotonic_ns() + 100_000_000
        return ack

    def _query(self, *args, **kwargs):
        rows = super()._query(*args, **kwargs)
        now = time.monotonic_ns()
        return [r for r in rows if now >= self.visible_at.get(r.id, 0)]


class AppendOldest(Markdown):
    name = "append-oldest"
    notes = "Append-only keyed writes. Keyed get returns the oldest value."

    def start(self, workdir, config):
        super().start(workdir, "append")

    def upsert(self, scope, record):
        return self.write(scope, record)

    def get(self, scope, key):
        return next((record for record in self._rows(scope) if record.key == key), None)


class SQLiteUpsert(SQLiteBusy):
    name = "sqlite-upsert"
    notes = "SQLite ON CONFLICT DO UPDATE. Keyed get returns the latest value."

    def start(self, workdir, config):
        super().start(workdir, config)
        self.db.executescript("""
            CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE ON records BEGIN
                DELETE FROM fts WHERE rowid=old.seq;
                INSERT INTO fts(rowid, text) VALUES(new.seq, new.text);
            END;
        """)

    def upsert(self, scope, record):
        if scope != record.scope:
            raise ValueError("Scope mismatch")
        self.db.execute(
            "INSERT INTO records"
            "(id,tenant,session,path,text,payload_sha256) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(tenant, session, path) DO UPDATE SET "
            "id=excluded.id, text=excluded.text, "
            "payload_sha256=excluded.payload_sha256",
            (
                record.id,
                scope.tenant,
                scope.session,
                record.key,
                record.text,
                record.payload_sha256,
            ),
        )
        return Ack(record.id, ref=Ref(record.id))


class SQLiteForgetFTS(SQLiteBusy):
    name = "sqlite-forget-fts"
    notes = "Delete removes the main row but leaves a readable FTS5 row."
    keep_fts = True

    def start(self, workdir, config):
        super().start(workdir, config)
        self.db.executescript("""
            DROP TRIGGER IF EXISTS records_ai;
            DROP TRIGGER IF EXISTS records_ad;
            DROP TABLE IF EXISTS fts;
            CREATE VIRTUAL TABLE fts USING fts5(
                id UNINDEXED, tenant UNINDEXED, session UNINDEXED,
                path UNINDEXED, text, payload_sha256 UNINDEXED
            );
            CREATE TRIGGER records_ai AFTER INSERT ON records BEGIN
                INSERT INTO fts(rowid,id,tenant,session,path,text,payload_sha256)
                VALUES(new.seq,new.id,new.tenant,new.session,new.path,new.text,new.payload_sha256);
            END;
        """)
        if not self.keep_fts:
            self.db.executescript("""
                CREATE TRIGGER records_ad AFTER DELETE ON records BEGIN
                    DELETE FROM fts WHERE rowid=old.seq;
                END;
            """)

    def search(self, scope, query, k, vector=None):
        escaped = '"' + query.replace('"', '""') + '"'
        rows = self.db.execute(
            "SELECT id,tenant,session,path,text,payload_sha256 FROM fts "
            "WHERE fts MATCH ? AND tenant=? AND session=? LIMIT ?",
            (escaped, scope.tenant, scope.session, k),
        )
        return [self._record(row) for row in rows]


class SQLiteForgetBoth(SQLiteForgetFTS):
    name = "sqlite-forget-both"
    notes = "Delete removes both the main row and its readable FTS5 row."
    keep_fts = False


class SQLiteResidueOff(SQLiteBusy):
    name = "sqlite-residue-off"
    notes = "secure_delete=OFF. Delete without VACUUM leaves plaintext residue."

    def start(self, workdir, config):
        super().start(workdir, config)
        self.db.execute("PRAGMA secure_delete=OFF")
        self.db.execute("INSERT INTO fts(fts, rank) VALUES('secure-delete', 0)")


class SQLiteResidueSecure(SQLiteBusy):
    name = "sqlite-residue-secure"
    notes = "secure_delete=ON. FTS5 secure-delete, VACUUM, and WAL truncation."

    def start(self, workdir, config):
        super().start(workdir, config)
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("INSERT INTO fts(fts, rank) VALUES('secure-delete', 1)")

    def delete(self, scope, ref):
        ack = super().delete(scope, ref)
        self.db.execute("VACUUM")
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return ack


class SQLiteDeleteDelayed(SQLiteBusy):
    name = "sqlite-delete-delayed"
    notes = "Delete ack precedes application by 50 ms."

    def start(self, workdir, config):
        super().start(workdir, config)
        self.pending_deletes = []

    def delete(self, scope, ref):
        self.pending_deletes.append((time.monotonic() + 0.05, scope, ref))
        return Ack(ref.id)

    def _query(self, *args, **kwargs):
        pending, self.pending_deletes = self.pending_deletes, []
        for deadline, scope, ref in pending:
            if time.monotonic() >= deadline:
                super().delete(scope, ref)
            else:
                self.pending_deletes.append((deadline, scope, ref))
        return super()._query(*args, **kwargs)
