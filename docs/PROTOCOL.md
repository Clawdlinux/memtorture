# Worker protocol v1

The harness has no runtime dependencies. Workers may use any language.
Each system supplies `systems/<name>/manifest.json`:

```json
{"command":["{python}","-m","memtorture.worker","sqlite"]}
```

`command` is an argv array, never a shell command.
`{python}` means the harness interpreter. Other arguments are passed unchanged.
Commands run from the harness working directory.
`start.args.workdir` is the cell's storage directory.

## Transport

Use UTF-8 JSON, 1 object per line, terminated by LF.
Stdout carries protocol lines only. Send logs to stderr.
The harness allows 1 outstanding request per worker.
Request IDs are integers. Replies echo the request ID.
Unknown fields are ignored at every nesting level.
Missing fields and wrong types invalidate the cell.
Malformed replies never enter the acknowledged-write set.
JSON must contain finite numbers. Booleans are not integers.

```json
{"id":1,"op":"capabilities","args":{}}
{"id":1,"result":["write","get"]}
{"id":2,"error":{"type":"not_supported","message":"search is unavailable"}}
```

Replies contain exactly 1 of `result` and `error`.
Errors require string `message`.
`error.type` is `not_supported`, `invalid_request`, `system_error`, or `timeout`.
An unsupported operation returns `not_supported`, not an empty success.
The worker may continue serving requests after an error.

Schemas live under `protocol/v1/`, with 1 file per operation and shared definitions in `common.json`.
Each operation schema accepts either its request or reply.
The harness uses hand-written stdlib checks, not a schema library.
ID equality, ack identity, and unique server roles and PIDs are additional relational checks.

## Values

Scopes are plain objects: `{"tenant":"t","session":"s"}`.
`QueryContext` is also plain. It requires `file_path`, `task_text`, `recent_context`, and nonnegative integer `token_budget`.

```json
{"kind":"record","id":"record-1","scope":{"tenant":"t","session":"s"},"key":"k","text":"café\ntext","payload_sha256":"harness digest"}
{"kind":"ref","id":"native-storage-id"}
{"kind":"ack","id":"record-1","dropped":false,"ref":{"kind":"ref","id":"native-storage-id"}}
```

A record requires every field shown.
Its optional `vector` is null or an array of 64 finite numbers.
The harness supplies vectors. Workers never embed text themselves.
`search` also receives the harness vector alongside the original query.
Non-vector workers can ignore vectors.
Workers return text unchanged, including Unicode and newlines. Never normalize or trim it.
Workers return the supplied digest. Only the harness checks or computes digests.
Malformed digest strings remain torn-record evidence, not protocol errors.

Every ack requires `kind`, string `id`, boolean `dropped`, and `ref`.
`ref` may be null when no native reference exists.
`write` and `upsert` ack IDs match the supplied record ID, even when dropped.
Only a validated, received, non-dropped ack counts as an acknowledged write.
`delete` ack IDs match the requested ref ID.
Delete a missing ref idempotently.

Refs are opaque. They always mean what that system's write or upsert returned.
Never substitute a record ID or key for a ref.
`get` reads a key. `get_by_ref` reads a native reference.
Both reads and deletes enforce scope isolation.
Reference lifetimes follow the native store. A keyed store may reuse a ref after replacement.

The payload digest is SHA-256 over canonical JSON containing `id`, `scope`, `key`, and `text`.
Keys are sorted recursively. Encode Unicode directly as UTF-8.
Use `, ` and `: ` separators, with no terminal newline.
Exclude `kind`, `payload_sha256`, and `vector`.
For example: `{"id": "r", "key": "k", "scope": {"session": "s", "tenant": "t"}, "text": "v"}`.
Workers treat this envelope as data. They do not reproduce Python serialization.

## Operations

Every request has `id`, `op`, and object `args`.
Counts and limits are nonnegative integers.

| Operation | Required args | Success result |
|---|---|---|
| `start` | `workdir`, `config` strings | Start object below |
| `capabilities` | None | String array |
| `write` | `scope`, `record` | Ack |
| `bulk_load` | `scope`, `records` array | null |
| `read_recent` | `scope`, `n` | Record array, newest first |
| `get` | `scope`, `key` | Record or null |
| `get_by_ref` | `scope`, `ref` | Record or null |
| `list_prefix` | `scope`, `prefix`, `limit` | Record array |
| `search` | `scope`, `query`, `k` | Record array |
| `upsert` | `scope`, `record` | Ack |
| `delete` | `scope`, `ref` | Ack |
| `data_dirs` | None | String array of storage paths |
| `retrieve_for` | `ctx` | Exact injection text |
| `stop` | None | null |
| `ensure_server_exited` | Positive integer `pid` | null |

`upsert` carries a full record, not an encoded JSON string.
The record's scope must match the request scope.
`bulk_load` is untimed setup. The harness validates loaded records separately.
`list_prefix` matches key prefixes, not record IDs.
`search.args.vector` is optional in the schema. Vector stores require it.
`stop` accepts optional `target: "all"` and `kill: false`.
It closes storage gracefully unless `kill` requests forced shutdown.
After replying to `stop`, a worker must accept stdin EOF and exit promptly.

```json
{"id":1,"result":{"protocol":1,"capabilities":["write","get_by_ref"],"version":"1.0","notes":"","servers":[{"role":"storage","pid":123}]}}
```

`start` requires integer `protocol: 1`, `capabilities`, string `version`, string `notes`, and `servers`.
Embedded stores report an empty server array.
Server roles are unique nonempty strings. PIDs are unique positive host process IDs.
The harness faults each role independently using `server:<role>`.
`both` faults the worker and every reported server.
`ensure_server_exited` waits for a reported PID to disappear, including reaping a child when needed.
It replaces `reap_server`. No language-specific process API crosses the protocol.
It returns `timeout` if the server remains alive after 10 seconds.
It returns `invalid_request` for an unreported PID.
The harness also checks process absence before restarting storage.

Capabilities name supported operations. `get-id` advertises the benchmark's native-ref read path.
`keyed` advertises native or documented emulated key lookup.
`emulated:<operation>` labels non-native mappings.
Tier 2 remains outside the current suites. Adapters may return `not_supported` for `retrieve_for`.

## Conformance kit

Run your worker from the repository root:

```sh
uv run memtorture conformance --command "node systems/reference-node/worker.mjs"
uv run memtorture conformance --command "python -m memtorture.worker sqlite" --config synchronous=NORMAL
```

`--command` uses shell-style argv quoting. It never invokes a shell.
Node 22 is sufficient. The reference worker imports only Node built-ins.
It stores records in memory. It makes no persistence claims.
It does not import harness code, compute digests, or embed text.
Native refs are fresh opaque UUID strings, not record IDs.

The kit replays `protocol/v1/golden.jsonl`.
Each line contains a request and an expected reply.
`$record:<id>` expands to a supplied record.
`$ref:<id>` binds to the native reference from that record's ack.
`$workdir` and `$config` describe per-run start arguments.
The harness creates both arguments for the worker's actual start request.
`match: "shape"` checks required reply fields instead of system-specific values.
`match: "capabilities"` also checks equality with the start capabilities.
Error messages may vary. Error types must match.
`requires` lists prerequisite capabilities for dependent transcript steps.

Start, capabilities, write, and stop are required.
Unadvertised operations may return `not_supported`.
Unsupported checks are printed as `SKIP`. They never count as passed coverage.
Advertised operations must work. Ref read and delete checks require native write refs.
The kit checks scope isolation, unchanged Unicode text, full-record upsert, and idempotent deletion.
Reported servers also receive per-role kill and exit checks before stop.
Keep protocol conformance separate from durability or benchmark success.

`malformed.jsonl` tests the harness validator before replay.
It includes missing tags, wrong ack IDs, invalid UTF-8, stray stdout, and extra fields.
The harness must reject malformed output and accept unknown fields.
Worker stdout receives the same checks during replay.
The kit creates a unique directory under `.conformance-work/` and removes it after completion.
