import { randomUUID } from "node:crypto";
import { createInterface } from "node:readline";

// This in-memory reference demonstrates the wire contract, not crash durability.
const capabilities = [
  "write", "bulk_load", "read_recent", "get", "get_by_ref", "list_prefix",
  "search", "upsert", "delete", "data_dirs", "retrieve_for", "keyed",
];
let rows = new Map();
let directory = "";

class RequestError extends Error {
  constructor(message, type = "invalid_request") {
    super(message);
    this.type = type;
  }
}

function check(test, message) {
  if (!test) throw new RequestError(message);
}

function scope(value) {
  check(value && typeof value.tenant === "string"
    && typeof value.session === "string", "Invalid scope");
}

function sameScope(a, b) {
  return a.tenant === b.tenant && a.session === b.session;
}

function record(value, expectedScope) {
  check(value && value.kind === "record", "Expected record kind");
  scope(value.scope);
  for (const field of ["id", "key", "text", "payload_sha256"]) {
    check(typeof value[field] === "string", `Invalid record ${field}`);
  }
  check(sameScope(value.scope, expectedScope), "Scope mismatch");
  // Digests and vectors are opaque harness data. No text conversion happens here.
  return {
    kind: "record", id: value.id,
    scope: { tenant: value.scope.tenant, session: value.scope.session },
    key: value.key, text: value.text, payload_sha256: value.payload_sha256,
  };
}

function ack(id, ref = null) {
  return { kind: "ack", id, dropped: false, ref };
}

function write(args, replace) {
  const value = record(args.record, args.scope);
  if (replace) {
    for (const [ref, row] of rows) {
      if (sameScope(row.scope, args.scope) && row.key === value.key) rows.delete(ref);
    }
  }
  const ref = `native:${randomUUID()}`;
  rows.set(ref, value);
  return ack(value.id, { kind: "ref", id: ref });
}

function dispatch(op, args) {
  check(args && typeof args === "object" && !Array.isArray(args), "Invalid args");
  if (op === "start") {
    check(typeof args.workdir === "string" && typeof args.config === "string",
      "start needs workdir and config");
    const openStart = process.hrtime.bigint();
    rows = new Map();
    directory = args.workdir;
    const open_ms = Number(process.hrtime.bigint() - openStart) / 1e6;
    return {
      protocol: 1, capabilities, version: "node-reference-v1", open_ms,
      notes: "In-memory reference. No persistence.", servers: [],
    };
  }
  if (op === "capabilities") return capabilities;
  if (op === "data_dirs") return [directory];
  if (op === "stop") return null;
  if (op === "retrieve_for") {
    check(args.ctx && Number.isInteger(args.ctx.token_budget), "Invalid context");
    return "";
  }
  if (op === "ensure_server_exited") throw new RequestError("Unreported server PID");
  if (!capabilities.includes(op)) throw new RequestError("Unsupported operation", "not_supported");
  scope(args.scope);
  const selected = [...rows.values()].filter(row => sameScope(row.scope, args.scope));
  if (op === "write" || op === "upsert") return write(args, op === "upsert");
  if (op === "bulk_load") {
    check(Array.isArray(args.records), "Expected records array");
    for (const value of args.records) write({ scope: args.scope, record: value }, false);
    return null;
  }
  if (op === "get_by_ref" || op === "delete") {
    check(args.ref?.kind === "ref" && typeof args.ref.id === "string", "Invalid ref");
    const found = rows.get(args.ref.id);
    const visible = found && sameScope(found.scope, args.scope);
    if (op === "get_by_ref") return visible ? found : null;
    if (visible) rows.delete(args.ref.id);
    return ack(args.ref.id);
  }
  if (op === "get") {
    check(typeof args.key === "string", "Invalid key");
    return selected.reverse().find(row => row.key === args.key) ?? null;
  }
  if (op === "read_recent") {
    check(Number.isInteger(args.n) && args.n >= 0, "Invalid n");
    return selected.reverse().slice(0, args.n);
  }
  if (op === "list_prefix") {
    check(typeof args.prefix === "string"
      && Number.isInteger(args.limit) && args.limit >= 0, "Invalid prefix or limit");
    return selected.filter(row => row.key.startsWith(args.prefix)).slice(0, args.limit);
  }
  check(typeof args.query === "string"
    && Number.isInteger(args.k) && args.k >= 0, "Invalid query or k");
  return selected.reverse().filter(row => row.text.includes(args.query)).slice(0, args.k);
}

const input = createInterface({ input: process.stdin, crlfDelay: Infinity, terminal: false });
for await (const line of input) {
  let request;
  let reply;
  try {
    request = JSON.parse(line);
    check(request && Number.isSafeInteger(request.id) && typeof request.op === "string",
      "Invalid request envelope");
    reply = { id: request.id, result: dispatch(request.op, request.args) };
  } catch (error) {
    reply = {
      id: request?.id ?? null,
      error: { type: error.type ?? "system_error", message: error.message },
    };
  }
  process.stdout.write(`${JSON.stringify(reply)}\n`);
}
