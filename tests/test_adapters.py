import time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from memproof.adapters import load_adapter
from memproof.types import NotSupported, Record, Ref, Scope

ROWS = [
    ("markdown", "append"),
    ("markdown", "rewrite"),
    ("sqlite", "synchronous=NORMAL"),
    ("sqlite", "synchronous=FULL"),
    ("sqlite-off", "default"),
    ("sqlite-busy", "default"),
    ("sqlite-visible", "default"),
    ("md-rmw-sleep", "default"),
]


@pytest.mark.parametrize("name,config", ROWS)
def test_shared_conformance(name, config, tmp_path):
    adapter = load_adapter(name)
    adapter.start(tmp_path, config)
    try:
        scope = Scope("tenant", "session")
        other = Scope("other", "session")
        record = Record.make(scope, "path/file", "needle0001 text")
        assert adapter.write(scope, record).id == record.id
        assert adapter.get(scope, record.key) == record
        assert adapter.get(scope, record.id) == record
        assert adapter.get(other, record.key) is None
        assert adapter.read_recent(scope, 20) == [record]
        assert adapter.list_prefix(scope, "path/", 20) == [record]
        assert adapter.search(scope, "needle0001", 20) == [record]
        assert adapter.search(other, "needle0001", 20) == []
        replacement = Record.make(scope, record.key, "replacement")
        assert (
            adapter.upsert(scope, record.key, replacement.value()).id == replacement.id
        )
        assert adapter.get(scope, record.key) == replacement
        adapter.delete(scope, Ref(replacement.id))
        assert adapter.get(scope, replacement.id) is None
        assert adapter.data_dirs() == [tmp_path]
        with pytest.raises(NotSupported):
            adapter.retrieve_for(None)
    finally:
        adapter.stop()


def test_unknown_config_fails_closed(tmp_path):
    for name in ("markdown", "sqlite"):
        with pytest.raises(NotSupported):
            load_adapter(name).start(tmp_path, "durable")


@given(st.text(), st.text(), st.text())
def test_hash_covers_scope_key_text(tenant, key, text):
    assert Record.make(Scope(tenant, "session"), key, text).valid()


@pytest.mark.parametrize("name", ["buffered", "delayed-index"])
def test_delayed_controls_start_invisible(name, tmp_path):
    adapter = load_adapter(name)
    adapter.start(tmp_path, "default")
    try:
        record = Record.make(Scope("t", "s"), "key", "needle")
        adapter.write(record.scope, record)
        assert adapter.get(record.scope, "key") is None
        time.sleep(0.13)
        assert adapter.get(record.scope, "key") == record
    finally:
        adapter.stop()
