from memproof.types import NotSupported


def load_adapter(name):
    from memproof.adapters.markdown import Markdown
    from memproof.adapters.sqlite import SQLite
    from memproof.controls.systems import (
        Buffered,
        DelayedIndex,
        RmwSleep,
        SQLiteBusy,
        SQLiteOff,
        SQLiteVisible,
    )

    adapters = (Markdown, SQLite, Buffered, DelayedIndex, RmwSleep)
    adapters += (SQLiteBusy, SQLiteOff, SQLiteVisible)
    for adapter in adapters:
        if adapter.name == name:
            return adapter()
    raise NotSupported(f"Adapter not implemented: {name}")
