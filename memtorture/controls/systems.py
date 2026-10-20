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
