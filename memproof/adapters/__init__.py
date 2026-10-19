from importlib import import_module

from memproof.types import NotSupported

ADAPTERS = {
    "markdown": ("memproof.adapters.markdown", "Markdown"),
    "sqlite": ("memproof.adapters.sqlite", "SQLite"),
    "qdrant": ("memproof.adapters.qdrant", "Qdrant"),
    "mcp-memory": ("memproof.adapters.mcp_memory", "MCPMemory"),
    "buffered": ("memproof.controls.systems", "Buffered"),
    "delayed-index": ("memproof.controls.systems", "DelayedIndex"),
    "md-rmw-sleep": ("memproof.controls.systems", "RmwSleep"),
    "sqlite-busy": ("memproof.controls.systems", "SQLiteBusy"),
    "sqlite-off": ("memproof.controls.systems", "SQLiteOff"),
    "sqlite-visible": ("memproof.controls.systems", "SQLiteVisible"),
}


def load_adapter(name):
    try:
        module, cls = ADAPTERS[name]
    except KeyError as exc:
        raise NotSupported(f"Unknown adapter: {name}") from exc
    return getattr(import_module(module), cls)()
