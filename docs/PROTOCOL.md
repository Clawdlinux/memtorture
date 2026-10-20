# Worker protocol v1

The harness has no runtime dependencies. Workers may use any language.
Each system supplies `systems/<name>/manifest.json`:

```json
{"command":["{python}","-m","memtorture.worker","sqlite"]}
```

A manifest may also carry `forget_promise` metadata:

```json
{"kind":"delete","url":"https://example.org/delete-doc","quote":"Deletes the record from reads."}
```

Store that object under the manifest's `forget_promise` key.
`kind` is `delete` or `erase`. `url` cites the documentation. `quote` repeats the documented promise.
This is harness metadata, not a worker operation or a required start-response field.
See [METHODOLOGY.md](METHODOLOGY.md#adapter-delete-promises) for the shipped adapter promises.

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
Non-vector stores must not persist unused vectors.
Markdown values exclude vectors. SQLite stores only its native columns and FTS text.
The Node reference store also discards unused vectors.
Vector stores keep vectors in their native index without duplicating them inside the text payload.
Workers return text unchanged, including Unicode and newlines. Never normalize or trim it.
Workers return the supplied digest. Only the harness checks or computes digests.
Malformed digest strings remain torn-record evidence, not protocol errors.

Every ack requires `kind`, string `id`, boolean `dropped`, and `ref`.
`ref` may be null when no native reference exists.
`write` and `upsert` ack IDs match the supplied record ID, even when dropped.
Only a validated, received, non-dropped ack counts as an acknowledged write.
`delete` ack IDs match the requested ref ID.
`delete` is idempotent. Missing and foreign-scope native refs always succeed as no-ops.
Its `dropped` boolean and `ref` value are informational.
`dropped` may be true or false. `ref` may be null or any valid ref.
The kit checks the delete ack's kind and ID, not those informational values.

Refs are opaque. They always mean what that system's write or upsert returned.
The harness never builds a ref from a record ID or key.
A worker may use its record ID as its native ref. The harness must still use the returned ref.
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
Search returns up to `k` records from the requested scope, ranked by the system's own relevance.
Results may include nearest neighbours without sharing query words.
The kit checks search membership and the rank of exact-text matches, never exact result-list equality.
An exact-text query must rank its own stored record first when `k` is positive.
Deleted and superseded records must be absent.
Search results must have unique record IDs.
Results must never exceed `k` or include another scope's records.
`k: 0` returns no records.
`stop` accepts optional `target: "all"` and `kill: false`.
It closes storage gracefully unless `kill` requests forced shutdown.
After a successful `stop` reply, a worker must accept stdin EOF and exit promptly with code 0.
Any other exit code is unclean. The harness kills its reported servers.
Malformed stop replies and trailing stdout invalidate the cell, including unterminated output.
The harness still kills and reaps a worker after shutdown protocol errors.
An unclean shutdown also kills every reported server and waits for PID absence.
Intentional worker-only faults can preserve servers.

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
The harness allows 12 seconds for that reply, so the worker's timeout can arrive first.
It returns `invalid_request` for an unreported PID.
The harness also checks process absence before restarting storage.

Capabilities name supported operations. `get_by_ref` enables the benchmark's native-ref read path.
Without that capability or a returned native ref, the path is N/A.
The harness never falls back to a key lookup using a record ID.
`keyed` advertises native or documented emulated key lookup.
`emulated:<operation>` labels non-native mappings.
`history` advertises retained versions per key.
The harness reports observed version counts separately. Versions are not duplicate record IDs.
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
Search reply arrays are valid examples, not exhaustive expected results.
`match: "search"` uses membership assertions with optional `first` and `absent` records.
`first` checks rank 1 only for an exact-text query. `absent` excludes deleted records by ID.
Other neighbours and their relevance order remain system-specific.
Error messages may vary. Error types must match.
`requires` lists prerequisites for dependent transcript steps.
An advertised or already successful operation satisfies a prerequisite.
Read checks allow 10 seconds for native visibility delay. They do not require immediate visibility after ack.

Start, capabilities, write, and stop are required.
Unadvertised operations may return `not_supported`.
Unsupported checks are printed as `SKIP`. They never count as passed coverage.
Advertised operations must work. Ref read and delete checks require native write refs.
The kit checks scope isolation, unchanged Unicode text, full-record upsert, and idempotent deletion.
Cross-tenant reads must be empty through every advertised read path.
After delete, every advertised path must exclude the deleted record.
After upsert, prefix lookup must return only the new record for that key.
Writes to distinct keys need distinct native refs. Each ref must resolve to its own record.
Unknown scope and record fields travel unchanged to the worker during replay.
Reported servers also receive per-role kill and exit checks before stop.
Keep protocol conformance separate from durability or benchmark success.

`malformed.jsonl` tests the harness reply decoder before replay.
It includes missing tags, wrong ack IDs, invalid UTF-8, stray stdout, and extra fields.
The harness must reject malformed output and accept unknown fields.
Worker stdout receives the same checks during replay.
The PASS line counts only checks against the worker.
The HARNESS line reports decoder self-checks separately.
The kit creates a unique directory under `.conformance-work/` and removes it on success.
Failures keep that directory and print its path plus the worker stderr tail.
Only the newest 10 failed directories under `.conformance-work/` are retained.
Older failures are deleted at each run's start and after a new failure.
Managed runs carry a harness PID marker. Active runs and directory symlinks are never pruned.
Custom work directories outside `.conformance-work/` remain caller-managed.
The tests reject 13 mutated Node workers, including stale superseded search entries and duplicate search results.

## Deterministic embedder

The pinned algorithm is `sha256-counter-v1`. It produces 64 binary64 values.
It has no seed, PRNG, model, or runtime-specific serialization.
It measures engine behavior, not semantic recall quality.
Do not normalize Unicode or change newlines before embedding.
Input must contain only valid Unicode scalar values.
Reject lone surrogates. Never replace them with U+FFFD before hashing.

1. Encode the exact input text as UTF-8, without a BOM or length prefix.
2. For counters 0 through 7, encode the counter as 4 unsigned big-endian bytes.
3. Hash `counter_bytes || text_bytes` with SHA-256.
4. Read each 32-byte digest as 8 signed big-endian 32-bit two's-complement integers.
5. Divide each integer by `2147483648.0` using IEEE-754 binary64 arithmetic.
6. Concatenate components in counter order, then digest byte order.
7. Starting at binary64 `0.0`, add each component's square from left to right.
8. Take the binary64 square root. Divide each component by that norm.

Do not use compensated summation, parallel reduction, or fused multiply-add.
Use round-to-nearest, ties-to-even binary64 arithmetic and correctly rounded square roots.
Output must match the published binary64 values bit for bit.
Serialize numbers with shortest round-trip JSON and compare decoded values exactly.
Equivalent JSON number spellings are allowed only when they decode to the same binary64 value.
`1e-15` is a diagnostic threshold, never a conformance tolerance.
INT32_MIN (`0x80000000`) maps to `-1.0` before normalization.
The harness computes vectors before sending operations.
Stored record digests exclude vectors, so vector transport never changes text integrity checks.

For an empty string, the first hash input is hex `00000000`.
For `café\n完整`, it is hex `00000000636166c3a90ae5ae8ce695b4`.
The following complete test vectors also ship as [embed-vectors.json](protocol/v1/embed-vectors.json).

```json
{
  "text": "",
  "first_digest": "df3f619804a92fdb4057192dc43dd748ea778adc52bc498ce80524c014b81119",
  "vector": [
    -0.05408267828975795, 0.0076963242938414295, 0.10624229159661876, -0.09867656920352652,
    -0.03555659212860806, 0.13661762025714985, -0.03959700579494079, 0.03421242461052526,
    -0.12544998267538704, -0.19082201101469135, 0.1844806505118133, 0.06591319145592911,
    0.026250002351790695, 0.17941867034274006, 0.007725781889898955, 0.19536004690834127,
    0.11103899592497607, -0.10528058287946214, -0.20275377497883323, 0.012604206030745479,
    0.16046183028493277, -0.14100334025077155, -0.06980599411892267, -0.06097520971850152,
    -0.19799376398257276, -0.18890686314765862, -0.04171473517708553, 0.012737782164765517,
    0.025024216483054314, -0.13991520560637408, -0.18970839760722907, 0.025555585761222677,
    0.045859913230638635, -0.05439713739196291, 0.12714987032332298, -0.18976193538086616,
    -0.1765786470476187, 0.03837947984806776, -0.04448152751260417, -0.18486966878693017,
    0.05634621695068923, 0.09109383337724886, 0.13075230695505488, 0.18681198966407508,
    -0.1398995381025701, -0.17126517138624334, 0.09227064532941719, 0.10347746899307171,
    -0.1282601415937464, 0.17726473275361154, 0.06658023592741687, -0.17979162781724797,
    0.14901242447863938, -0.17444668545879397, -0.17488361055239085, 0.20476528745267447,
    0.03530646284074793, 0.16200614109446634, 0.11346914253357089, 0.05215414839067022,
    -0.12944759391735908, 0.08351982035443463, 0.020229190457279292, 0.06710577907836503
  ]
}
```

```json
{
  "text": "café\n完整",
  "first_digest": "6a311a51acf794d09bd02dc9861759eb3b631d55a6d6b8f78d16af86ce9e6acd",
  "vector": [
    0.17877276901020012, -0.13978497188775388, -0.1686634051144062, -0.2052321301443955,
    0.09997765728097022, -0.15010199111513187, -0.19345208235001957, -0.08313269146970717,
    0.0976838595812481, 0.09310115875070106, 0.172103180357574, -0.09946853993410788,
    0.12868392646996657, 0.020009667847772358, 0.10897922414054856, 0.023089351409177106,
    0.01688220469246233, -0.07288134022222545, -0.21331203747883146, 0.026497157124060807,
    -0.1536691711498261, 0.08298580011680687, 0.07576593911941999, 0.06713496802883101,
    -0.12662736073199277, -0.14737935701277038, -0.06001192543334896, -0.05157299196774899,
    -0.05534077938761292, 0.20169538367574605, -0.10315258414796633, 0.12441936378748351,
    -0.04975513605471421, -0.05281387475859145, -0.016770330007411096, -0.03457619327379946,
    -0.1445592115343599, 0.1658093558810098, 0.050476847352977194, 0.1359029216008765,
    0.011073725619577366, 0.13684167779958625, 0.18947920368317997, -0.03733587068733315,
    -0.1982043609576388, 0.0464965695364935, 0.02067784042122505, -0.011865012786245806,
    0.202061328911911, -0.21211553082156678, -0.19027428073407238, -0.16327011759662263,
    0.09576443679795489, 0.06098596089957986, 0.20194078803630655, 0.1526585571648116,
    -0.054051847579816084, -0.1438730880607413, 0.061502386986340585, -0.009083830897923962,
    0.18280190341037902, -0.18682655946765991, 0.08491567912334338, -0.06756073100183506
  ]
}
```

These extra vectors cover 4-byte UTF-8 and an 87-byte, multi-block input:

```json
{
  "text": "🚀",
  "first_digest": "1b3067c2949dbc62b1b142ac174f6bf8fb48ae2dcc23c1f87bd589abc34652d6",
  "vector": [
    0.04630149403011373, -0.1828687044648757, -0.13335344016123937, 0.039696042199071906,
    -0.008031242436008248, -0.08831523843728173, 0.21088262787812104, -0.10341180040820717,
    -0.21036430081961077, -0.1756106085760722, 0.005420126714446315, -0.1551263713363525,
    0.01819097024708947, 0.14069760321008598, 0.09793668555204027, -0.17651990578125976,
    -0.049550641482987504, -0.07040790753626905, 0.14335062095188328, 0.009755266747401362,
    0.06824448144641979, -0.21357160725578628, 0.13890148905989483, 0.050116116155399404,
    0.06545557309832359, 0.18199711515986913, -0.06146362962674113, -0.192660979145084,
    0.01974662205551478, -0.11362703128770395, 0.14149010594615252, -0.06548859915466364,
    0.03805148093269053, -0.020596780036364535, -0.2141042079239356, -0.10914017495048202,
    -0.03736635020760064, -0.21769851856635364, 0.16241262325037095, -0.20682250569634497,
    0.10599873010512449, -0.0094071768091241, 0.13618551333323975, 0.15253927923401198,
    0.15497268363325464, 0.17793575961040717, 0.11816366874426822, -0.052953397466315326,
    -0.025732956499852005, 0.12373368155536524, -0.14318129523058462, 0.042729188002996445,
    -0.09537442402086771, 0.004509854873712717, 0.07715211044062942, 0.06363375636134522,
    -0.08745027050288276, -0.21236766560552833, 0.030583068295273835, -0.054097751653267405,
    0.19702931139542773, -0.17954869658225756, -0.015029539282834908, 0.1321264179597519
  ]
}
```

```json
{
  "text": "memory:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "first_digest": "9b3468915849baa474c560476900b747ed5bdc7fa8ace513482268f76eb4bda1",
  "vector": [
    -0.17538758488906966, 0.15362445453610585, 0.20318594129926193, 0.18270882329307964,
    -0.03243633266619185, -0.15194814650364888, 0.1255165991170666, 0.19263264436546237,
    0.13969811500044918, 0.041262608891035765, 0.12358226051857109, -0.16212581741749957,
    -0.052009494945034235, -0.1347895007683868, -0.17830510679766193, -0.11810011476344903,
    -0.15081710566181045, -0.05239943810570808, 0.19249475792923584, 0.0025705022709390365,
    -0.028333782251920205, -0.043557961508920065, 0.02067258611886559, 0.08820305836939837,
    -0.11219451471106158, -0.006036801789017127, 0.005777615223076225, 0.1731947637374932,
    0.0255474411296633, 0.20826768430370904, 0.1100725443433802, 0.036397900252967935,
    -0.13396906038156106, -0.13070489220689713, 0.18984969936888307, -0.05170895426128737,
    -0.1524926922574003, 0.11515065319776642, 0.06902528591289069, -0.006084704325792655,
    -0.043005079819752205, -0.05360482811944683, -0.15775967174862301, -0.13097661990940654,
    -0.2153988686903975, -0.0406929579432808, -0.005883704713128023, 0.11014018143510099,
    0.14260469981827323, -0.06870802388657636, 0.042043801909204724, -0.21457452947454525,
    0.1435618914880031, 0.022175321585327368, 0.19817456120650995, 0.00487464633756933,
    -0.20081538728884235, 0.02343155796333387, 0.10768830087135003, 0.13323912308811334,
    0.10674249050085614, -0.08701061325210789, 0.2155724658366885, 0.004873504753958599
  ]
}
```

The standalone Node verifier reads a JSON array of texts from stdin:

```sh
printf '["","café\\n完整"]' | node systems/reference-node/embed.mjs
```

It reproduces vectors without Python or harness imports.
The Node reference worker does not import the verifier.
Workers consume supplied vectors instead of computing their own.
