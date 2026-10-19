from importlib import import_module

from memtorture.types import NotSupported

ADAPTERS = {
    "markdown": ("memtorture.adapters.markdown", "Markdown"),
    "sqlite": ("memtorture.adapters.sqlite", "SQLite"),
    "qdrant": ("memtorture.adapters.qdrant", "Qdrant"),
    "mcp-memory": ("memtorture.adapters.mcp_memory", "MCPMemory"),
    "buffered": ("memtorture.controls.systems", "Buffered"),
    "delayed-index": ("memtorture.controls.systems", "DelayedIndex"),
    "md-rmw-sleep": ("memtorture.controls.systems", "RmwSleep"),
    "sqlite-busy": ("memtorture.controls.systems", "SQLiteBusy"),
    "sqlite-off": ("memtorture.controls.systems", "SQLiteOff"),
    "sqlite-visible": ("memtorture.controls.systems", "SQLiteVisible"),
}


def load_adapter(name):
    try:
        module, cls = ADAPTERS[name]
    except KeyError as exc:
        raise NotSupported(f"Unknown adapter: {name}") from exc
    return getattr(import_module(module), cls)()
