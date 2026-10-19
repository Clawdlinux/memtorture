import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol


class NotSupported(Exception):
    """An operation or configuration has no supported mapping."""


@dataclass(frozen=True)
class Scope:
    tenant: str
    session: str


@dataclass(frozen=True)
class Record:
    id: str
    scope: Scope
    key: str
    text: str
    payload_sha256: str
    vector: list[float] | None = field(default=None, compare=False)

    @classmethod
    def make(cls, scope: Scope, key: str, text: str, id: str | None = None):
        from memtorture.embed import embed

        record = cls(id or str(uuid.uuid4()), scope, key, text, "")
        return cls(record.id, scope, key, text, record.digest(), embed(text))

    def digest(self) -> str:
        payload = asdict(self)
        del payload["payload_sha256"]
        del payload["vector"]
        data = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(data).hexdigest()

    def valid(self) -> bool:
        return self.digest() == self.payload_sha256

    def value(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_value(cls, scope: Scope, key: str, value: str):
        data = json.loads(value)
        data["scope"] = Scope(**data["scope"])
        record = cls(**data)
        if record.scope != scope or record.key != key or not record.valid():
            raise ValueError("Invalid keyed payload envelope")
        return record


@dataclass(frozen=True)
class Ref:
    id: str


@dataclass(frozen=True)
class Ack:
    id: str
    dropped: bool = False
    ref: Ref | None = None


@dataclass(frozen=True)
class QueryContext:
    file_path: str
    task_text: str
    recent_context: str
    token_budget: int


class Adapter(Protocol):
    name: str

    def capabilities(self) -> set[str]: ...
    def start(self, workdir: Path, config: str) -> None: ...
    def stop(self, target: str = "all", kill: bool = False) -> None: ...
    def write(self, scope: Scope, record: Record) -> Ack: ...
    def read_recent(self, scope: Scope, n: int) -> list[Record]: ...
    def get(self, scope: Scope, key: str) -> Record | None: ...
    def get_by_ref(self, scope: Scope, ref: Ref) -> Record | None: ...
    def list_prefix(self, scope: Scope, prefix: str, limit: int) -> list[Record]: ...
    def search(
        self, scope: Scope, query: str, k: int, vector: list[float] | None = None
    ) -> list[Record]: ...
    def upsert(self, scope: Scope, record: Record) -> Ack: ...
    def delete(self, scope: Scope, ref: Ref) -> Ack: ...
    def retrieve_for(self, ctx: QueryContext) -> str: ...
    def data_dirs(self) -> list[Path]: ...
