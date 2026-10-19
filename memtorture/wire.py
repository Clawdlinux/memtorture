"""Protocol v1 shapes. JSON Schemas document the same checks."""

import math
from dataclasses import fields

from memtorture.types import Ack, QueryContext, Record, Ref, Scope

OPERATIONS = {
    "start",
    "capabilities",
    "write",
    "bulk_load",
    "read_recent",
    "get",
    "get_by_ref",
    "list_prefix",
    "search",
    "upsert",
    "delete",
    "data_dirs",
    "retrieve_for",
    "stop",
    "ensure_server_exited",
}
ERROR_TYPES = {"not_supported", "invalid_request", "system_error", "timeout"}
KINDS = {"record": Record, "ack": Ack, "ref": Ref}


class ProtocolError(RuntimeError):
    """Malformed worker output invalidates the cell."""


def require(condition, message):
    if not condition:
        raise ProtocolError(message)


def strings(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def scope(value):
    require(isinstance(value, dict), "Expected a plain scope object")
    require(
        all(isinstance(value.get(k), str) for k in ("tenant", "session")),
        "Scope needs tenant and session strings",
    )


def vector(value):
    require(
        isinstance(value, list)
        and len(value) == 64
        and all(type(v) in (int, float) and math.isfinite(v) for v in value),
        "Expected 64 finite vector components",
    )


def tagged(value, kind):
    require(isinstance(value, dict) and value.get("kind") == kind, f"Expected {kind}")
    require(isinstance(value.get("id"), str), f"{kind} needs a string id")
    if kind == "record":
        scope(value.get("scope"))
        require(
            all(
                isinstance(value.get(k), str) for k in ("key", "text", "payload_sha256")
            ),
            "Record needs key, text and payload_sha256 strings",
        )
        # Bad digests are torn-record evidence, not malformed protocol output.
        if value.get("vector") is not None:
            vector(value["vector"])
    if kind == "ack":
        require(type(value.get("dropped")) is bool, "Ack needs a dropped boolean")
        require("ref" in value, "Ack needs ref, or null")
        if value["ref"] is not None:
            tagged(value["ref"], "ref")


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    kind = value.get("kind")
    if isinstance(kind, str) and kind in KINDS:
        tagged(value, kind)
        cls = KINDS[kind]
        body = {f.name: value[f.name] for f in fields(cls) if f.name in value}
        if cls is Record:
            body["scope"] = Scope(
                **{k: value["scope"][k] for k in ("tenant", "session")}
            )
        elif cls is Ack:
            body["ref"] = decode(value["ref"])
        return cls(**body)
    return value


def decode_args(args):
    require(isinstance(args, dict), "args must be an object")
    body = dict(args)
    for name in ("record", "records", "ref"):
        if name in body:
            body[name] = decode(body[name])
    if "scope" in body:
        scope(args["scope"])
        body["scope"] = Scope(**{k: args["scope"][k] for k in ("tenant", "session")})
    if "ctx" in body:
        ctx = body["ctx"]
        require(isinstance(ctx, dict), "ctx must be an object")
        require(
            all(
                isinstance(ctx.get(k), str)
                for k in ("file_path", "task_text", "recent_context")
            )
            and type(ctx.get("token_budget")) is int
            and ctx["token_budget"] >= 0,
            "Invalid query context",
        )
        body["ctx"] = QueryContext(
            **{f.name: ctx[f.name] for f in fields(QueryContext)}
        )
    return body


def validate_request(request):
    require(isinstance(request, dict), "Request must be an object")
    require(type(request.get("id")) is int, "Request needs an integer id")
    op, args = request.get("op"), request.get("args")
    require(isinstance(op, str), "op must be a string")
    require(isinstance(args, dict), "args must be an object")
    scoped = {
        "write",
        "upsert",
        "bulk_load",
        "get",
        "get_by_ref",
        "delete",
        "read_recent",
        "list_prefix",
        "search",
    }
    if op in scoped:
        scope(args.get("scope"))
    for name in {
        "start": ("workdir", "config"),
        "get": ("key",),
        "list_prefix": ("prefix",),
        "search": ("query",),
    }.get(op, ()):
        require(isinstance(args.get(name), str), f"{name} must be a string")
    for name in {
        "read_recent": ("n",),
        "list_prefix": ("limit",),
        "search": ("k",),
        "ensure_server_exited": ("pid",),
    }.get(op, ()):
        value = args.get(name)
        require(
            type(value) is int and value >= (1 if name == "pid" else 0),
            f"Invalid {name}",
        )
    if op in {"write", "upsert", "bulk_load"}:
        records = args.get("records") if op == "bulk_load" else [args.get("record")]
        require(isinstance(records, list), "records must be an array")
        for record in records:
            tagged(record, "record")
            require(
                all(
                    record["scope"][k] == args["scope"][k]
                    for k in ("tenant", "session")
                ),
                "Record scope mismatch",
            )
    if op in {"get_by_ref", "delete"}:
        tagged(args.get("ref"), "ref")
    if op == "search" and "vector" in args:
        vector(args["vector"])
    if op == "retrieve_for":
        require("ctx" in args, "retrieve_for requires ctx")
    if op == "stop":
        require(args.get("target", "all") == "all", "Invalid stop target")
        require(type(args.get("kill", False)) is bool, "kill must be boolean")


def validate_reply(reply, token, op, args):
    require(isinstance(reply, dict), "Reply must be an object")
    require(type(reply.get("id")) is int and reply["id"] == token, "Reply id mismatch")
    require(("error" in reply) != ("result" in reply), "Expected result or error")
    if "error" in reply:
        error = reply["error"]
        require(
            isinstance(error, dict)
            and isinstance(error.get("type"), str)
            and error["type"] in ERROR_TYPES
            and isinstance(error.get("message"), str),
            "Invalid error",
        )
        return
    result = reply["result"]
    if op == "start":
        require(isinstance(result, dict), "start result must be an object")
        require(
            type(result.get("protocol")) is int and result["protocol"] == 1,
            "Expected protocol 1",
        )
        require(strings(result.get("capabilities")), "Invalid capabilities")
        require(
            all(isinstance(result.get(k), str) for k in ("version", "notes")),
            "start needs version and notes",
        )
        servers = result.get("servers")
        require(isinstance(servers, list), "start needs servers")
        roles, pids = set(), set()
        for server in servers:
            require(isinstance(server, dict), "Invalid server")
            role, pid = server.get("role"), server.get("pid")
            require(
                isinstance(role, str) and bool(role) and role not in roles,
                "Server roles must be unique nonempty strings",
            )
            require(
                type(pid) is int and pid > 0 and pid not in pids,
                "Server PIDs must be unique positive integers",
            )
            roles.add(role)
            pids.add(pid)
    elif op in {"write", "upsert", "delete"}:
        tagged(result, "ack")
        expected = args["ref"].id if op == "delete" else args["record"].id
        require(result["id"] == expected, "Ack does not match request payload")
    elif op in {"get", "get_by_ref"}:
        if result is not None:
            tagged(result, "record")
    elif op in {"read_recent", "list_prefix", "search"}:
        require(isinstance(result, list), "Expected record array")
        for record in result:
            tagged(record, "record")
    elif op in {"capabilities", "data_dirs"}:
        require(strings(result), f"{op} requires a string array")
    elif op == "retrieve_for":
        require(isinstance(result, str), "retrieve_for requires exact text")
    elif op in {"bulk_load", "stop", "ensure_server_exited"}:
        require(result is None, f"{op} requires null")
    else:
        raise ProtocolError(f"Unknown operation: {op}")
