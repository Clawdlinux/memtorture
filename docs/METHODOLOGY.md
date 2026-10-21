# B1 methodology

Sponsored and built by the Clawdlinux team.

This is evidence, not marketing.
There is no combined score or leaderboard.
Comparative publication remains blocked pending outside review and the 14-day reply window.

## Process model

The harness stays outside the tested worker.
Every adapter runs in a separate Python process.
Messages use JSON lines over stdin and stdout.
Workers never print logs on the protocol channel.
The harness captures worker stderr outside the system data directory, in a sibling `logs/` directory.
Conformance runs keep data under `data/` and diagnostics under `logs/`.
Harness diagnostics cannot make plaintext residue appear measurable.

A write is acked only after the harness reads its reply.
The storage call must return before that reply.
An unread reply is not an ack.
In-flight writes may survive or disappear.
Their presence is not a violation.

The harness hashes the record before sending it.
The hash covers the id, scope, key, and text.
Readback recomputes SHA-256 over the same canonical JSON.
Hash failures count as torn records.
Missing acked records count as losses.

Keyed writes pass a full record through protocol v1.
The harness never builds a ref from a record ID.
A system may use its record ID as its native ref.
See [PROTOCOL.md](PROTOCOL.md) for wire shapes and validation rules.
Adapters map that envelope onto native keyed operations where available.
Scope includes both tenant and session.
Every read filters both.

## Timing samples

Every percentile group reports `n`, its sample count.
The p50 uses the nearest rank when at least 1 sample exists. Empty groups report null.
The p99 uses the nearest rank only when `n >= 100`. Smaller groups report null, not an estimated tail.
This applies to recall, history, and throughput latency summaries.

## Fault targets

| Target | Meaning |
|---|---|
| `worker` | Kill the Python application worker. |
| `server` | Kill the native storage server PID. |
| `both` | Kill the server PID, then the worker PID. |

The harness uses `os.kill(pid, SIGKILL)` directly.
It does not call `docker kill`.
After a server kill, the harness waits until its PID no longer exists.
The wait has a 10-second deadline.
The worker reaps its dead server child before restart.
Server faults work on macOS because the servers are native processes.
Worker-only faults say little about server-backed storage.
Process kills test user-space buffering and recovery, not disk durability.
The kernel page cache survives.
Power-loss testing is outside B1.

The MCP adapter is disabled by its security scan.
No Node server was executed.
Its planned stdio mapping would require separate Node children sharing one backing file.
Qdrant clients share one server per data directory.

## Controls

Run `uv run memtorture controls` before measuring cells.
It uses 20 trials per control.
`--quick` uses 5.
Evidence is keyed by platform under `results/controls/<os>-<arch>/`.
The key lowercases `platform.system()` and normalizes `platform.machine()`.
Architecture names are matched without case.
`aarch64` and `arm64` map to `arm64`.
`amd64` and `x86_64` map to `x86_64`.
Other architecture names stay lowercase.
Examples are `darwin-arm64` and `linux-x86_64`.
OS versions are not part of the key.
The full gate lives at `results/controls/<os>-<arch>/gate.json`.
Full evidence lives in that platform directory's `full/` subdirectory.
Quick runs write only to that platform directory's `quick/` subdirectory.
They never replace the full gate or authorize benchmark cells.
Runs never replace another platform's evidence.
Mac evidence lives under `results/controls/darwin-arm64/`.
Full and quick evidence are regenerated after committed methodology changes.
A missing platform gate blocks gated cells and names the required path.
Gated suites require a current full gate with at least 20 trials.
Must-fail controls must fail in at least 90% of trials.
Must-pass controls must pass every trial.
The threshold is never rounded down.

| Suite | Must fail | Must pass |
|---|---|---|
| `process-kill` | User-space buffer. Flush interval 100 ms. | SQLite WAL, `synchronous=OFF`, commit before ack. |
| `concurrent-append` | Markdown rewrite with a 5 ms race window. | SQLite WAL with a 30-second busy timeout. |
| `concurrent-kv` | The same isolated rewrite race. | The same SQLite busy timeout. |
| `recall-delay` | Fixed 100 ms visibility delay, polled every 1 ms. | SQLite visible on commit. |
| `supersede-by-key` | Append-only store whose keyed read returns the oldest value. | SQLite WAL with keyed upsert. |
| `forget` | SQLite deletes the main row but leaves a readable FTS5 row. | SQLite deletes both rows. |
| `forget-residue` | SQLite secure deletion disabled, without VACUUM. | SQLite secure deletion enabled, FTS5 secure-delete enabled, VACUUM, then WAL truncation. |

The ungated measurement entries are `history-scale`, `write-throughput`, and `recovery`.
They have no fault controls.
Each gate entry has `discriminates: null` and `gated: false`.
Measurement results carry that note instead of borrowing another suite's rates.
History reads also verify loaded counts, hashes, keys, and recency order.

The supersession control upserts key `k` with values `v1` through `v5`.
Each version has a different record ID.
A decoy-key upsert follows each `k` version, including the last version.
The final read must return `k`'s latest record, not the last row written overall.
There are 10 acknowledged writes per 5-version check.
It reads `k` and passes only when every write succeeds and the final value is intact `v5`.
The append-only control keeps every version but returns `v1`.
SQLite updates the keyed row in place with `ON CONFLICT DO UPDATE` and returns `v5`.
An update trigger removes the old FTS entry and inserts the new text within the same transaction.
`check_supersede_by_key(worker, *, versions=5, timeout_s=5, poll_ms=10, emit=None)` provides the reusable check in `memtorture/suites.py`.
It accepts a running protocol worker and emits writes plus the final check.
The supersession suite reuses this check for each system and config row.

The delayed control stages index entries behind a monotonic deadline.
This avoids measuring the operating system's timer coalescing.
For 1 ms calibration polls, the harness spins until each scheduled poll.
Normal recall cells use 10 ms sleeping polls.
Calibration retains its 100 ms lower bound and 101 ms plus read overhead upper bound.
Read overhead uses p99 when at least 100 reads exist. Otherwise it uses the maximum observed read latency.
The gate records that choice and the read sample count. It never invents a p99 from fewer reads.
SQLite must be visible on the first read.

The first Mac calibration used `threading.Timer`.
It detected the delay in only 20% of 5 trials.
Observed lag reached 105.479 ms despite a 100 ms delay.
Timer coalescing and sleeping polls measured scheduling delay instead.
Logical visibility deadlines and precise calibration polls fixed that cause.
The 90% gate and timing bounds did not change.

A later quick run detected the delay in only 80% of 5 trials.
The harness paused 35.436 ms between reads.
The following RPC took only 0.109 ms.
Result logging ran inside that gap.
A slow-sink regression reproduced the timing failure.
Recall now buffers raw events until each observation finishes.
Logging no longer sits inside the measured polling window.

Controls rotate concurrency through 2, 4, and 8 processes.
Read calibration rotates through id, filter, recency, and search.
Raw operations and exact outcomes live under each platform's `full/` and `quick/` evidence directories.
Controls run on the current machine.
Code changes or machine changes invalidate only the current platform gate.
The gate still checks the exact OS description and hardware.
An invalid gate blocks cell generation and reporting.

## Suites

### Recall delay

Each supported path gets 20 independent writes.
Independent writes keep each path's polling budget separate.
The harness polls every 10 ms for at most 60 seconds.

- `write_latency` runs from sending the write through reading its ack.
- `visibility_lag` runs from the ack through successful readback.
- `time_to_visible` includes both.

Timeouts and dropped writes are separate counts.
Each poll records its latency and integrity result.
Unsupported paths show N/A.
Native write references travel in the ack when supported.
The id path uses only `get_by_ref`. It never treats a record ID as a key.
Missing support or a missing write ref makes that path N/A.
The Qdrant id path retrieves that point reference with 1 native call.
It does not hash the logical record id or perform a filtered-scroll fallback.
Latency is observed read completion time, not the engine's internal index timestamp.

### History scale

One session grows to 1K, 10K, and 100K records.
`--extended` adds 1M.
Setup is untimed.
All records include text and scope fields used by the supported indexes.

The JSONL transport has an untimed `bulk_load` setup extension.
It is not a new required Adapter method.
Adapters may use native batches.
The fallback calls the approved `write` method for every record.
Markdown appends setup batches without scanning or rewriting existing rows.
SQLite uses a transaction.
Qdrant waits for setup batches to apply, regardless of the timed row.

Each size measures 20 repeats of recency, keyed get, and known-record search.
Recency must return the actual last 20 records.
The suite reports nearest-rank p50 and p99 per size and operation.
It does not set a performance pass threshold.

### Process kill

Each fault target gets 20 trials.
The fixed workload seed is `20261019`.
Each trial writes between 1 and 8 records.
The random write prefix selects the fault point.
The harness kills immediately after the final ack.
It does not sleep or write logs between that ack and the kill.
Logging happens after the fault with the original operation timestamps.
The harness restarts against the same data directory.
Readback counts acked missing records, duplicates, and torn payloads.

### Concurrent append

Separate worker processes append 20 distinct records each.
Runs use 2, 4, and 8 workers.
A harness barrier starts each group.
Only replies read by the harness enter the expected set.
The final scan counts missing records, duplicates, and torn payloads.
Malformed Markdown lines retain unique line-number ids.
Prefix readback keeps invalid rows even when the valid-row limit is reached.

### Concurrent keyed writes

Each group writes distinct keys, then overlapping keys.
Distinct keys must all survive.
Each overlapping key must end with one intact acked value.
Rows are checked for duplicate record IDs, not duplicate keys.
Only intact rows count toward duplicates.
Corrupt copies count as torn, even when their IDs match intact records.
Every returned row must match an acked write.
Multiple intact versions of a key are allowed.
Malformed rows count as torn.
History reports the advertised `history` capability separately from observed versions per key.
Observed versions count distinct intact acked record IDs per key.
The first intact row for an ID is retained. Later corrupt copies cannot hide that version.
History never changes pass or fail.
CAS remains "not measured in B1".
Emulated upsert is marked.

### Write throughput

Each config row runs 1, 8, and 64 writer processes.
Each process owns a protocol worker. A harness thread drives each worker.
A barrier releases the writers after every worker starts and every record is prepared.
Each writer sends 20 records by default. `--extended` sends 100.
Record text sizes are 128, 1024, or 8192 UTF-8 bytes.
Seed `20261020` samples these sizes with weights 6:3:1.
Payloads repeat ASCII `x`. This measures the stated sizes, not corpus compression ratios or semantic retrieval.
The seed resets for each writer count. This keeps workloads identical across config rows.

Throughput counts non-dropped acknowledgements per second, from barrier release through the last acknowledgement.
Write latency runs from harness send through validated acknowledgement receipt.
Results report latency percentiles in milliseconds, separately for each writer count, with the sample-count rules above.
Each writer count also reports `window_ms`, from barrier release through the last acknowledgement.
Raw worker-start events record every process's spawn and start-reply timestamps.
The window starts after every start reply. Process startup never contributes to throughput.
Record preparation, worker startup, and readback are outside the throughput window.
Raw events are buffered until measurement finishes. Logging cannot extend that window.

Readback enumerates distinct writer keys after the last acknowledgement.
It allows up to 5 seconds for asynchronous visibility, polling every 10 ms.
Results retain acknowledged-but-missing, duplicate, and torn-record counts beside throughput.
Dropped writes have their own count. They do not inflate ops/s.
A fast lossy store therefore keeps its losses visible.
This suite is ungated. Loss counts do not change its measurement status.
There are no fault controls or performance thresholds.
Missing write or enumeration support produces N/A.
This does not measure unlimited saturation, disk durability, or application scheduling overhead outside the harness window.

### Supersede by key

Each config row runs `check_supersede_by_key` with 5 versions.
The control procedure above is the suite body, including interleaved decoy-key writes.
It reports whether the current keyed value is correct and intact.
An older intact value or no value is retried every 10 ms for up to 5 seconds.
This lets asynchronous upserts converge.
It fails only when that budget expires. A torn or wrong record fails on the first read.
Each cell records `visible_after_ms` from the last write ack, `reads`, and the budget.
Control trials use a 100 ms budget polled every 1 ms.
Missing keyed, upsert, or get support produces N/A.
The append-oldest and SQLite-upsert controls gate this capability check.
It does not time individual upserts or claim atomic compare-and-swap behavior.
It does not require history retention or measure concurrent supersession.

### Forget

The suite writes 3 survivor records and a target.
It obtains the native reference from the target's write acknowledgement.
It deletes that reference through the native API, without deriving a ref from the key or record ID.

Every supported path is queried: get, get_by_ref, list_prefix, read_recent, and exact-text search.
Each path must first return the intact target. This prevents empty or unreadable paths from falsely passing.
After deletion, paths are polled together until the target disappears or 5 seconds pass.
The lag clock starts when the harness receives the delete reply.
Each path reports absence, remaining matches, and observed lag in milliseconds.
The lag includes polling intervals and read completion time, not an internal deletion timestamp.
`wait=false` is therefore allowed to converge instead of failing an immediate visibility check.

A missing operation is N/A for that path.
Missing delete support or a missing write ref makes the suite N/A.
A supported path without pre-delete visibility is not measurable, not a pass.
A timeout with the target still visible is a violation.
Survivors must still read intact on every measured path, and collateral loss is a violation reported as `survivors_missing`.

The row carries its documented promise, URL, and exact quote from the system manifest.
Delete promises require absence from supported reads. They do not promise disk erasure.
Erase promises additionally require a measurable plaintext scan before deletion and no matches after shutdown.
An unreadable pre-delete plaintext makes that erase judgement not measurable.
This is a plaintext check, not proof against forensic recovery or backups.
The shipped adapters only promise delete.

#### Adapter delete promises

Markdown removes the scoped record through a file rewrite. It promises absence from reads, not secure disk erasure.

SQLite deletes the scoped row and its FTS5 entry in one transaction. It promises absence from reads, not disk erasure.

Qdrant v1.16 [delete-points documentation](https://api.qdrant.tech/v-1-16-x/api-reference/points/delete-points)
says: "Deletes specified points from the collection."
Its `wait` parameter says: "If true, wait for changes to actually happen".
This is a delete promise, not an erase promise.
The disabled MCP adapter remains N/A. We do not invent a delete promise for an unexecuted adapter.

#### Forget controls

These controls promise absence from supported reads. Fault injection deliberately breaks that promise in the must-fail store.

The must-fail store deletes the main SQLite row but leaves its independently readable FTS5 row.
That FTS5 row retains the original record envelope, so search can return the deleted target.
An FTS join through the main table would hide this fault. The control does not use that join.
The must-pass store deletes both rows within the same transaction.
Control trials use a 100 ms visibility budget. Ordinary benchmark cells use 5 seconds.
The fixed fault never converges, so shortening calibration does not change the expected result.
A deterministic asynchronous delete scenario checks lag reporting through real protocol workers.
Native Qdrant suite scenarios are opt-in: `uv run pytest -q tests/test_memory_lifecycle.py --slow -m slow`.
The default test run skips them.

### Forget residue

Each trial creates a fresh random 32-hex plaintext token inside the target's text.
Before deletion, the harness scans every file under `data_dirs` as raw bytes.
No file extension filter is applied. SQLite WAL and journal files are included.
Overlapping directory reports are deduplicated. Chunk boundaries preserve token matches.

If the token is absent, the result is not measurable and deletion is not attempted.
The storage may be compressed, encoded, or not yet persisted in scan-readable form.
Absence is never evidence of erasure without that positive pre-delete observation.
There is no separate compression control or adapter-name classification.

Otherwise, the suite runs the same read-path deletion check and stops the system cleanly.
It scans those data paths again after shutdown.
Results include occurrence counts before and after deletion, plus matched bytes after deletion.
Bytes found equals post-delete occurrences multiplied by 32.
The token and record text never enter suite events or stdout logs.

The ordinary SQLite rows get no extra cleanup. Their default delete behavior is the measurement.
Markdown's native delete already rewrites its file.
Qdrant receives only its documented delete call. Background optimization is not forced or awaited beyond deletion visibility.
No snapshots, external backups, unallocated filesystem blocks, or device-level copies are scanned.
Zero plaintext matches does not prove secure erasure or unlearned model behavior.
Residue is a suite violation, not a claim that a delete-only promise was broken.
`forget` remains the separate judgement of that documented promise.

The must-fail control disables SQLite secure_delete and FTS5 secure-delete, without VACUUM.
The must-pass control enables both, runs VACUUM after delete, then runs wal_checkpoint(TRUNCATE).
That cleanup belongs to the control, not the ordinary SQLite rows.
Both controls gate measurable residue checks. Not measurable never counts as either control succeeding.
Missing native delete support or a missing write ref produces N/A, as in forget.

### Recovery

Each config row bulk-loads 100K records without timing, then stops cleanly.
`--extended` also runs an independent 1M-record dataset.
The reopened dataset uses the same data directory and config row.
Readiness ends when a known middle record first reads back correctly, including its payload digest.
The result separates these timing fields:

- `spawn_to_start_ms`: harness time from spawning the worker through receiving its start reply.
- `open_ms`: the worker's measured duration of the system's open call, carried in the start reply.
- `start_to_first_read_ms`: harness time from the start reply through the first valid get.
- `unattributed_start_ms`: `spawn_to_start_ms - open_ms`, or null when open telemetry is absent.
- `recovery_ms`: the headline metric, `open_ms + start_to_first_read_ms`, unless timing is anomalous.
- `time_to_ready_ms`: total harness time from spawn through that first valid get.

The total equals `spawn_to_start_ms + start_to_first_read_ms`.
The cell labels open timing with `open_ms_source: "self-reported"`.
Unattributed time includes startup and transport. It may also include storage work omitted from the worker's reported open duration.
The harness does not independently verify that duration.
If `open_ms > spawn_to_start_ms`, the size result and cell carry `timing_anomaly: "open_ms exceeds spawn_to_start_ms"`.
The headline becomes null. The negative unattributed value remains visible for diagnosis.
Storage verification status remains separate from the timing anomaly flag.
Open time includes the adapter's native setup and any server readiness waits inside its open call.
The Python worker and Node reference report `open_ms` from their own monotonic clocks.
Third-party workers may omit it. Then `open_ms`, `unattributed_start_ms`, and `recovery_ms` are null, with other timings still measured.
Missing open telemetry means the headline is N/A. It never becomes zero or falls back to total process startup.
A missing known record after 60 seconds produces an error, not a readiness number.
This is warm filesystem-cache recovery after clean shutdown, not cold boot or crash recovery.

RSS is sampled immediately after readiness, before enumeration allocates readback records.
In-process stores report the worker PID's RSS.
Server-backed stores report every role from the start response's servers list, without adding worker RSS.
Linux reads VmRSS from `/proc/<pid>/status`. macOS uses `ps -o rss= -p <pid>`.
Results convert KiB to bytes and retain per-role values.
Other platforms produce N/A for this RSS measurement.
RSS is resident process memory, not peak memory, proportional set size, or total machine use.

Readback samples up to 100 evenly spaced records, including endpoints when the sample count exceeds 1.
Each sample must match its expected record and digest.
When list_prefix exists, a full enumeration also checks the count, unique IDs, and every returned digest.
Enumeration requests disjoint sub-prefix pages, each covering up to 10K expected records, and sums their counts.
For 1M records, prefixes run from `key0000` through `key0099`.
The 10001-record limit detects extras inside queried pages, not records in unqueried pages beyond the loaded range.
No single reply must carry the whole dataset. The verification also reports the number of enumeration pages.
The JSONL receiver uses an extending bytearray and searches each new chunk once for its newline.
This keeps framing linear for large replies. The 64 MiB protocol regression stays in the default test run.
The count is an enumeration count, not a native COUNT API. Protocol v1 has no COUNT operation.
Without enumeration support, count fields are null and the source is N/A. Sampling is still reported.
Missing get support makes readiness N/A because no known-record probe is available.
Sample loss or a count mismatch produces a violation with the verification counts retained.
This cannot prove unsampled payload correctness when the count matches.
There are no fault controls. Recovery is explicitly ungated.

## Rows and mapping

| System | Rows | Mapping |
|---|---|---|
| Markdown | `append`, `rewrite` | Flat Markdown. Substring search. Keyed rewrites and read scans are marked. |
| SQLite | `synchronous=NORMAL`, `synchronous=FULL` | WAL, FTS5, path column, native keyed replacement. |
| MCP memory | `default` | N/A. Dependency scan returned `DO_NOT_INSTALL` with incomplete coverage. |
| Qdrant | `wait=false`, `wait=true` | Native keyed points, scope indexes, and ordered recency. Prefix matching is local. |

Rows use exact settings.
Qdrant recency prefers a harness-supplied sequence when available.
Without that sequence, wall time orders independent workers.
There is no Qdrant row named `durable`.
The approved design's example uses that nickname.
Its row-naming rule takes precedence.
`wait` controls when writes are applied and visible, not fsync.
Qdrant deletes use 1 filter RPC matching the native point ID, tenant, and session.
Deletes never depend on a visibility read, including `wait=false`.
The [v1.16 API](https://api.qdrant.tech/v-1-16-x/api-reference/points/upsert-points)
says: "If true, wait for changes to actually happen".

SQLite's [synchronous documentation](https://www.sqlite.org/pragma.html#pragma_synchronous)
describes WAL sync behavior.
`NORMAL` does not sync the WAL on every transaction.
`FULL` syncs the WAL after each transaction commit.
Markdown and MCP have no documented durable mode.

Vector operations use `sha256-counter-v1`, with 64 dimensions.
It hashes a big-endian counter followed by exact UTF-8 text.
Signed 32-bit words become normalized binary64 components.
The byte layout and cross-language test vectors are in [PROTOCOL.md](PROTOCOL.md).
The harness computes record and query vectors. Workers never embed text.
Non-vector stores discard supplied vectors before persistence.
Markdown and its controls store only the original record envelope, without vector components.
SQLite and its controls keep the native record columns and FTS text, without vector storage.
Protocol schemas, vectors, and worker manifests contribute to the control gate's methodology hash.
It measures engine behavior, not semantic recall quality.
The optional realistic embedder is outside B1.

## Outputs and governance

`operations.jsonl` records individual writes, reads, faults, and checks.
`manifest.json` records the command, machine, OS, versions, config rows, and harness commit.
Dirty worktrees are identified.
Control manifests capture source provenance before opening output files.
Generated files under `results/controls/` do not mark the harness source dirty.
Other source and documentation changes still do.
Commit code before regenerating evidence, then commit only the generated evidence.
The methodology fingerprint identifies the exact code used.
Run output goes only under `results/runs/`. Control evidence goes only under `results/controls/<os>-<arch>/`.
Paths are resolved through symlinks and `..` before the check. Other paths are rejected.
While governance blocks publication, the harness writes raw results only, never multi-system tables.
`memtorture report --local` prints per-suite tables to stdout with a LOCAL ONLY banner. It writes no files.
Tables sort systems alphabetically.
`memtorture export-system` copies one system's cells, operations, harness commit and platform. Other systems' numbers are left out.
Reports and exports never change raw operations.

Only control results are committed.
Comparative results remain gitignored.
An outside maintainer must approve methodology before comparative publication.
Each upstream project gets 14 days to review its adapter and output.
Maintainer config fixes are accepted even when they change the sponsor's standing.
Sponsor marketing must link the full run.
Corpus and results use CC BY 4.0.

## Spec decisions

The README names the sponsor's product, as the approved design requires.
There are no sponsor-database adapters or cells.

The plan asks every suite to have controls.
The design only specifies failure controls for fault and visibility suites.
History runs ungated without an invented control or slowdown threshold.

The design's Qdrant example uses `--config durable`.
Its explicit row rule requires `wait=false` or `wait=true`.
The CLI rejects the nickname.
