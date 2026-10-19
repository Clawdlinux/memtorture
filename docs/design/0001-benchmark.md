# 0001: memtorture, a benchmark for agent memory engines

Status: Approved after 3 review rounds (Claude Opus 5.5 and Gemini 4 Argon, independent). Renamed from `memproof` to `memtorture` on 2026-10-19. `membench` was rejected because it collides with the MemBench agent-memory paper (arXiv 2506.21605).

## Why

Agent memory products mostly publish recall scores on LoCoMo and LongMemEval. Several independent reruns did not reproduce vendor numbers, and the vendors' own notes say some scores came from hosted builds rather than the open-source code (sources in `docs/research-landscape-2026-10-18.md`).

Users also report problems that no harness measures as a number:

- acked writes that are later missing (for example mem0 #5245, graphiti #1707, and modelcontextprotocol/servers #4797, fixed in `server-memory` 2.0.0 with a lock file and atomic rename);
- long delays between a write and when it can be recalled (mem0 #2813, graphiti #186, Zep and Redis docs);
- slowdown as history grows;
- concurrent writers losing updates (mem0 #4892, cognee #5254);
- deletes that miss copies (mem0 #7452);
- outdated facts still returned after an update;
- coding agents repeating mistakes they were corrected on.

No completed crash-safety test exists for any agent memory store or vector database (research, 2026-10-18).

memtorture measures these, reproducibly, with raw outputs, controls, and suites where any system can lose, including the sponsor's.

## Sponsor disclosure

memtorture is built by the team behind zerodb. That is stated at the top of the README and on every results page. The governance rules below exist to make the results trustworthy anyway.

## Principles

- **Evidence, not claims.** Every number ships with the command, pinned versions, hardware, and raw per-operation output.
- **Same interface for everyone.** No system gets a special path. Anyone can add a system or rerun a cell.
- **Controls first.** Each fault suite has a configuration that must fail and one that must pass. A suite whose controls do not behave is not reported.
- **No LLM where a program can check.** Tier 1 uses no LLM and a deterministic embedder. LLM-judged quality is delegated to existing harnesses.
- **Strongest native mapping.** Each adapter uses the system's own APIs: keyed upsert, point delete, metadata filters, durability flags. No emulation that makes a system look worse. Emulated or unsupported cells are marked as such.
- **Two rows per system where it matters:** documented default config, and the documented durable config if one exists (otherwise marked "no documented durable mode"), each with its write latency and throughput.
- **OSS only.** Self-hosted open-source builds. No hosted clouds (several cloud terms bar publishing benchmarks).

## What it does not do

- Rebuild LoCoMo or LongMemEval runners. Supermemory's `memorybench` (MIT) already does that through a provider interface. We contribute a zerodb provider there.
- Benchmark ANN speed on static corpora. VectorDBBench does that.
- Publish a combined score or a leaderboard.

## Process model

- Every system under test runs in its own worker process (and its own server container if it has one). The harness talks to the worker over a pipe.
- A write is **acked** only when the harness has read the worker's reply after the system's call returned.
- Every payload carries a content hash written by the harness, so torn records are detected the same way for every system.
- Fault targets are reported separately: application worker, storage server, both.
- For client-server systems the main fault target is the server. The harness kills the server PID directly with `os.kill(pid, SIGKILL)` right after the ack. It never shells out to `docker kill`, whose delay can hide a short async flush window. Containers are resolved to a host PID with `docker inspect`. That only works on Linux, so the server fault target is Linux-only. On macOS, servers run as native binaries. Killing only the client worker of a server-backed system tests little, and results say so.
- Like Jepsen's `:info`, writes that were in flight and not acked may be present or absent. Only acked writes missing, or torn records, are violations.

## Tier 1: engine properties (no LLM, deterministic, CI)

All vector systems use one pinned deterministic embedder (a seeded hash embedder in CI, `BAAI/bge-small-en-v1.5` via fastembed for realistic runs). Setup data is bulk loaded without timing. Default scale caps at 100K records; `--extended` goes to 1M.

| Suite | What it does | Metrics |
|---|---|---|
| `write-throughput` | 1, 8 and 64 writers, mixed record sizes | ops/s, p50/p99 write latency, per config row |
| `recall-delay` | Write a record, then read it back through each documented read path (by id, by metadata filter, by recency, by search). Poll interval 10 ms, timeout 60 s. Records an extraction step dropped (Mem0 `infer=True`) are reported as `dropped`, apart from timeouts | `write_latency` (start to ack), `visibility_lag` (ack to visible), `time_to_visible` (start to visible), per read path. Timeouts reported separately |
| `history-scale` | One session grows to 1K, 10K, 100K (1M extended). Every record is written so it is reachable by every read path the system supports, so index sizes match across systems. At each size: last 20 by recency, get by key, search for a known record | p50/p99 per operation and size, as curves. Non-native operations marked |
| `process-kill` | Acked writes, then SIGKILL of each fault target at random points, restart, read everything | Acked writes lost, torn records. Tests user-space buffering and recovery, not disk durability, and says so |
| `power-loss` (Linux) | Same workload on LazyFS. Order: SIGKILL the system, then clear the LazyFS cache, then restart. Systems run as native processes at the same pinned versions as the containers, in a subdirectory of the LazyFS mount, never in Docker over FUSE | Acked writes lost, torn records |
| `concurrent-append` | 2, 4, 8 processes append distinct records to one scope | Acked records missing, duplicates, torn records |
| `concurrent-kv` | 2, 4, 8 processes write distinct keys, then overlapping keys | Distinct keys: none missing. Overlapping: final value equals one intact acked write. CAS support and history are reported as capabilities, never as pass or fail |
| `supersede-by-key` | Native keyed update N times, read | Capability check: current value correct |
| `forget` | Delete through the native API, then query every documented read path | Judged against the system's own documented promise (delete vs erase) |
| `forget-residue` | After `forget`, scan the data directory for the plaintext | Bytes found. zerodb v0.1 fails this until compaction ships |
| `recovery` | Restart after 100K and 1M records | Time to ready, RSS after open |

Controls. Each suite has its own must-fail and must-pass control. Each control isolates one cause. A must-fail control must fail in at least 90% of trials, and the rate is published. If a control does not discriminate, the suite publishes nothing. One exception: the `mmap` pair in `power-loss` only gates `mmap`-based systems.

| Suite | Must fail | Must pass |
|---|---|---|
| `process-kill` | A toy writer that acks, then buffers in user space and flushes every 100 ms | SQLite WAL `synchronous=OFF`, ack after commit. The page cache survives SIGKILL, and the result shows it |
| `power-loss` | SQLite WAL `synchronous=OFF`, ack after commit. A toy writer that does `pwrite` without `fsync`. A toy writer that writes through `mmap` without `msync` | SQLite `synchronous=FULL`, ack after commit. The toy writer with `fsync`. The `mmap` writer with `msync` before ack |
| `concurrent-*` | Markdown read-modify-write with a fixed 5 ms sleep inside the window | SQLite with a busy timeout |
| `recall-delay` | A wrapper that indexes after a fixed 100 ms async delay. Run with a 1 ms poll. Measured `visibility_lag` must fall between 100 ms and 100 ms plus 1 ms plus the read p99 | SQLite, visible on commit |
| `forget` | A store that deletes the main row but leaves the FTS5 row | SQLite deleting both |
| `forget-residue` | SQLite delete with `secure_delete=OFF` and no `VACUUM` | SQLite with `secure_delete=ON`, FTS5 `secure-delete` on, then `VACUUM` and `wal_checkpoint(TRUNCATE)` |

SQLite WAL `synchronous=NORMAL` is reported in `power-loss` as an expected-loss row. If the `mmap` must-fail control passes, LazyFS cannot see `mmap` writes. Then every `mmap`-based system is reported as "power-loss not measurable", never as a pass.

## Tier 2: correction recall for coding agents

### Retrieval level (no LLM, CI)

- **Same input for every system.**
  - Records are `Correction(id, path_scope, task_tags, text, supersedes_id)`.
  - Queries are `QueryContext(file_path, task_text, recent_context, token_budget)`.
  - Adapters may use metadata filters, text, or both. Upstream maintainers may rewrite their adapter.
- **Corpus, published before any zerodb result:**
  - Mined from the git history of `CLAUDE.md`, `AGENTS.md`, `.cursor/rules` and `.github/instructions` in permissively licensed public repos, with attribution. Later edits give supersession ground truth.
  - Stratified, each bucket scored separately:
    - A, path or directory scoped (about 35%);
    - B, task, library or symbol scoped, where path globs cannot help (about 35%);
    - C, supersession and revocation (about 30%).
  - Paraphrased wording is included.
  - Labeled by people outside the zerodb team, with inter-annotator agreement reported.
  - Queries come from the corpus, not from us. For each correction, the `QueryContext` is built from a later commit in the same repo: its touched file paths and its commit message as task text. Outside labelers mark which corrections that commit should have seen. The method ships with the corpus.
  - Bucket C runs in 2 modes, scored apart. **Keyed:** `supersedes_id` is given. **Unkeyed:** only the new text and `path_scope` are given, and the system must work out what changed.
  - Split into a public `dev` set and a frozen, checksummed `eval` set. Adapters must not branch on eval contents.
- **Tokens:**
  - Counted by the harness, never the adapter, with `o200k_base`, over the exact string that would be injected, including wrappers.
  - Recall, precision and stale rate are reported at fixed budgets of 500, 1K and 2K tokens.
  - Output over the budget is cut at the budget by the harness, and only what is left is scored. The overflow rate per system is reported.

### End-to-end level (LLM, opt-in)

- **Harness:** one open, minimal tool-loop agent, the same injection hook and budget for every system. No closed coding-agent CLIs.
- **Two sub-suites:**
  - **apply:** corrections are preloaded identically for every system;
  - **capture:** each system's own write path, reported separately.
- **Tasks:** on public repos. Programmatic checks only: tests pass, a forbidden pattern is absent.
- **Models:** a pinned hosted snapshot at temperature 0 with a fixed seed, plus one open-weights model run locally so results stay reproducible after hosted models change.
- **Runs:** at least 5 repeats per task, with confidence intervals. Full transcripts and cost per run are published.

## Tier 3: recall quality (delegated)

A zerodb provider in `memorybench`. memtorture links those results. It does not re-host LoCoMo (CC BY-NC).

## Systems

| System | Tier | Rows | Notes |
|---|---|---|---|
| Markdown, flat + `rg` | 1, 2 | append, rewrite | What developers use today |
| Markdown, nested `CLAUDE.md` / `AGENTS.md` | 2 | curated, append | Directory scoping for free |
| Rules files (`.instructions.md`, `.mdc`) | 2 | | Path globs |
| SQLite (WAL + FTS5, path column) | 1, 2 | `synchronous=NORMAL`, `synchronous=FULL` | The 50-line DIY baseline |
| MCP reference memory server | 1, 2 | | What MCP users try first |
| Qdrant | 1, 2 | `wait=false`, `wait=true` | `wait` controls when a write is applied and visible, not fsync. Rows are named by the setting, never "durable". Metadata filters allowed |
| Chroma (self-hosted) | 1, 2 | default | |
| Mem0 OSS | 1, 2 | `infer=False`; `infer=True` with pinned local LLM | LLM rows labeled and capped at 1K records |
| Graphiti OSS | 1 (small N), 2 | pinned local LLM via Ollama | Capped at 1K records |
| basic-memory | 2 | | MCP over markdown with SQLite index |
| zerodb | 1, 2 | | Only from a tagged release with `zerodb serve` (zerodb S3). Never through a shim |

Rows are named by their exact setting. Each row cites the upstream doc line that says what is persisted before the call returns. A system with no documented fsync-before-ack mode gets "no documented durable mode" instead of a second row.

## Architecture

- Python harness, `uv` with a lockfile.
- One adapter file per system, plus a pinned config. Upstream maintainers can own their adapter through `CODEOWNERS`.

```python
class Adapter(Protocol):
    name: str
    def capabilities(self) -> set[str]: ...
    def start(self, workdir: Path, config: str) -> None: ...      # config row, named by its exact setting
    def stop(self, target: str = "all", kill: bool = False) -> None: ...
    def write(self, scope: Scope, record: Record) -> Ack: ...
    def read_recent(self, scope: Scope, n: int) -> list[Record]: ...
    def get(self, scope: Scope, key: str) -> Record | None: ...
    def list_prefix(self, scope: Scope, prefix: str, limit: int) -> list[Record]: ...
    def search(self, scope: Scope, query: str, k: int) -> list[Record]: ...
    def upsert(self, scope: Scope, key: str, value: str) -> Ack: ...
    def delete(self, scope: Scope, ref: Ref) -> Ack: ...
    def retrieve_for(self, ctx: QueryContext) -> str: ...           # Tier 2: the exact text to inject
    def data_dirs(self) -> list[Path]: ...                          # for forget-residue
```

- Unsupported methods raise `NotSupported`, which shows in results as N/A.
- Results:
  - raw per-operation JSONL;
  - per-suite tables (systems in alphabetical order);
  - a manifest with versions, configs, hardware, OS and harness commit.
- `memtorture run --suite process-kill --system qdrant --config durable` reproduces one cell.
- Published results come from scripted CI or a documented machine, never hand runs. A methodology change reruns every system.

## Governance

- Sponsor disclosure on the README and every results page.
- At least one maintainer from outside the sponsor before the first comparative publication. Methodology changes need their approval.
- 14-day right of reply. Before publishing comparative tables, open an issue with each upstream project with the adapter, config and raw output. Link the issue and record who replied.
- Maintainer config fixes are merged even when they lower zerodb's standing.
- Expected zerodb losses are written down before zerodb's first run.
- Sponsor marketing must link the full run, never a cropped table.

## Milestones

| M | Scope | Publishes |
|---|---|---|
| B1 | Harness, process model, controls. Suites: `recall-delay`, `history-scale`, `process-kill`, `concurrent-append`, `concurrent-kv`. Adapters: markdown (flat), SQLite (both rows), MCP reference memory server, Qdrant (both rows) | Methodology, code and control results. Comparative tables only after an outside maintainer joins and the 14-day reply window closes. No zerodb cells |
| B2 | `write-throughput`, `recovery`. `power-loss` on Linux with LazyFS (GitHub Actions or a Linux VM), with the `mmap` controls. `supersede-by-key`, `forget`, `forget-residue`. Adapters: Chroma, Mem0 (`infer=False`) | Results. Tier 2 corpus published for critique |
| B3 | Tier 2 retrieval level. Adapters: nested markdown, rules files, basic-memory, SQLite FTS5 | Tier 2 results, once the outside maintainer and the corpus labels are in |
| B4 | zerodb adapter (after zerodb S3 is tagged). Mem0 `infer=True` and Graphiti with a pinned local LLM, small N | zerodb cells, after the 14-day reply window |
| B5 | Tier 2 end-to-end. zerodb provider for `memorybench` | End-to-end results |

B1 is worth open-sourcing alone. It would be the first public process-kill and concurrency test of agent memory stores and vector databases, on systems the sponsor does not own. The governance rules apply from B1, not from B4.

## Expected zerodb losses (pre-registered)

- `search` and Tier 2 bucket B: N/A or weak until zerodb has search.
- `forget-residue`: fails until compaction.
- `write-throughput` with 1 writer: likely slower than stores that ack before fsync in their default rows.
- `recovery`: open is O(log size) for the CRC scan.
- Tier 3 recall quality: likely below LLM-extraction systems.

## Licensing

- Code: Apache-2.0.
- Corpus and results: CC BY 4.0.
- Mined rules files only from permissively licensed repos, with attribution.

## Open questions

1. GitHub org. Resolved: `Clawdlinux/memtorture`, with sponsor disclosure in the README.
