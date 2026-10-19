import json
import sys
from types import SimpleNamespace

import pytest

from memtorture.protocol import ProtocolError, Worker, decode, encode
from memtorture.suites import read_path, recall_delay
from memtorture.types import NotSupported, Record, Scope


def test_id_path_never_substitutes_a_record_id_for_a_key():
    record = Record.make(Scope("t", "s"), "key", "text")
    calls = []
    worker = SimpleNamespace(
        capabilities={"get", "get-id"},
        refs={},
        call=lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    with pytest.raises(NotSupported):
        read_path(worker, "id", record)
    assert not calls


@pytest.mark.parametrize("capabilities", [{"get", "get-id"}, {"get_by_ref"}])
def test_recall_id_path_without_capability_or_ref_is_na(
    monkeypatch, tmp_path, capabilities
):
    class NoRefWorker:
        info = {}
        refs = {}
        last_sent_ns = 1
        last_ack_ns = 2

        def __init__(self, *args):
            self.capabilities = capabilities

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def call(self, op, **args):
            from memtorture.types import Ack

            assert op == "write", "No id, key, or ref read is allowed without a ref"
            return Ack(args["record"].id)

    monkeypatch.setattr("memtorture.suites.Worker", NoRefWorker)
    result = recall_delay("probe", "default", tmp_path, paths=("id",), samples=1)
    assert result["paths"]["id"]["status"] == "N/A"


@pytest.mark.parametrize("kind", ["record", ["record"], {"unexpected": "tag"}])
def test_plain_scope_and_unknown_fields_never_decode_kind_tags(kind):
    record = Record.make(Scope("t", "s"), "key", "text")
    value = encode(record)
    value["future"] = {"kind": kind}
    value["scope"]["kind"] = kind
    value["scope"]["future"] = {"nested": {"kind": kind}}
    assert decode(value) == record
    assert decode({"future": {"kind": kind}}) == {"future": {"kind": kind}}


@pytest.mark.parametrize(
    "stop_reply,trailing",
    [
        ({"id": 2, "result": {}}, b""),
        ({"id": 2, "result": None}, b"late log\n"),
        ({"id": 2, "result": None}, b"partial log"),
        ({"id": 2, "result": None}, b'{"id":3,"result":null}\n'),
    ],
)
def test_shutdown_protocol_errors_fail_and_reap_worker(tmp_path, stop_reply, trailing):
    start = {
        "id": 1,
        "result": {
            "protocol": 1,
            "capabilities": [],
            "version": "probe",
            "notes": "",
            "servers": [],
        },
    }
    script = (
        "import json,sys,time; sys.stdin.readline(); "
        f"print({json.dumps(start)!r},flush=True); "
        "sys.stdin.readline(); "
        f"print({json.dumps(stop_reply)!r},flush=True); "
        f"time.sleep(.03); sys.stdout.buffer.write({trailing!r}); "
        "sys.stdout.buffer.flush(); sys.stdin.read()"
    )
    worker = Worker(
        "probe", "default", tmp_path, command=[sys.executable, "-c", script]
    )
    with pytest.raises(ProtocolError):
        worker.close()
    assert worker.process.poll() is not None
    assert worker.log.closed
    assert worker.process.stdout.closed
