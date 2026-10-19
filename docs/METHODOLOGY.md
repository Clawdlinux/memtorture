# B1 methodology

Sponsored and built by the Clawdlinux team.

This is evidence, not proof.
There is no combined score or leaderboard.
Comparative publication remains blocked pending outside review and the 14-day reply window.

## Process model

The harness stays outside the tested worker.
Every adapter runs in a separate Python process.
Messages use JSON lines over stdin and stdout.
Workers never print logs on the protocol channel.

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

Keyed writes pass the record envelope through the protocol's string value.
Adapters map that envelope onto native keyed operations where available.
Scope includes both tenant and session.
Every read filters both.

## Fault targets

| Target | Meaning |
|---|---|
| `worker` | Kill the Python application worker. |
| `server` | Kill the native storage server PID. |
| `both` | Kill the server PID, then the worker PID. |

The harness uses `os.kill(pid, SIGKILL)` directly.
It does not call `docker kill`.
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

Run `uv run memproof controls` before measuring cells.
It uses 20 trials per control.
`--quick` uses 5.
Quick runs write only under `results/controls/quick/`.
They never replace the top-level full gate or authorize benchmark cells.
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

History scale is an ungated measurement suite.
No controls are defined for it in the approved spec.
Its gate entry has `discriminates: null` and `gated: false`.
Results carry that note instead of borrowing another suite's rates.
History reads also verify loaded counts, hashes, keys, and recency order.

The delayed control stages index entries behind a monotonic deadline.
This avoids measuring the operating system's timer coalescing.
For 1 ms calibration polls, the harness spins until each scheduled poll.
Normal recall cells use 10 ms sleeping polls.
Calibration retains the approved bounds: 100 ms through 101 ms plus read p99.
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
Raw control operations and exact outcomes live under `results/controls/`.
Controls run on the current machine.
Code changes or machine changes invalidate the gate.
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
Markdown uses an equivalent file batch.
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
Intact rows are checked for duplicate keys.
Every returned row must match an acked write.
Malformed rows count as torn, not duplicate keys.
CAS and history report "not measured in B1", not false capability measurements.
Emulated upsert is marked.

## Rows and mapping

| System | Rows | Mapping |
|---|---|---|
| Markdown | `append`, `rewrite` | Flat Markdown. Substring search. Keyed rewrites and read scans are marked. |
| SQLite | `synchronous=NORMAL`, `synchronous=FULL` | WAL, FTS5, path column, native keyed replacement. |
| MCP memory | `default` | N/A. Dependency scan returned `DO_NOT_INSTALL` with incomplete coverage. |
| Qdrant | `wait=false`, `wait=true` | Native keyed points, scope indexes, and ordered recency. Prefix matching is local. |

Rows use exact settings.
There is no Qdrant row named `durable`.
The approved design's example uses that nickname.
Its row-naming rule takes precedence.
`wait` controls when writes are applied and visible, not fsync.
The [v1.16 API](https://api.qdrant.tech/v-1-16-x/api-reference/points/upsert-points)
says: "If true, wait for changes to actually happen".

SQLite's [synchronous documentation](https://www.sqlite.org/pragma.html#pragma_synchronous)
describes WAL sync behavior.
`NORMAL` does not sync the WAL on every transaction.
`FULL` syncs the WAL after each transaction commit.
Markdown and MCP have no documented durable mode.

Vector operations use seeded hash embedder v1.
It has 64 dimensions and seed `20261019`.
It measures engine behavior, not semantic recall quality.
The optional realistic embedder is outside B1.

## Outputs and governance

`operations.jsonl` records individual writes, reads, faults, and checks.
`manifest.json` records the command, machine, OS, versions, config rows, and harness commit.
Dirty worktrees are identified.
The methodology fingerprint identifies the exact code used.
Per-suite Markdown tables sort systems alphabetically.
Tables are local evidence only.
`memproof report` never changes raw operations.

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
