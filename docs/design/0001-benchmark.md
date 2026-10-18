# 0001: memproof, a benchmark for agent memory engines

Status: Draft for review. Working name `memproof`.

## Why

Agent memory products publish recall scores on LoCoMo and LongMemEval, and independent reruns often fail to reproduce them. Mem0's co-founder confirmed the paper numbers came from the SaaS platform, not the OSS code. Redis could not reproduce several vendors' scores.

What users complain about most is never measured:

1. Acked writes that are lost (Mem0 #5245 silent drops, Graphiti #1707, the MCP memory server overwriting concurrent writers).
2. Time from write to recallable: Mem0 `add` about 20 s (#2813), Graphiti about 12 s per message (#186), Zep "a few seconds to a few minutes", Redis Agent Memory extracts every 5 minutes by default.
3. Slowdown as history grows.
4. Concurrent writers losing updates (Mem0 #4892, Cognee #5254).
5. Deletes that miss copies (Mem0 #7452).
6. Old facts still answered after an update.
7. Coding agents repeating mistakes they were corrected on.

Research (2026-10-18, `memory-bench-landscape.md`) found no harness that measures 1 to 7. No completed crash-safety test exists for any agent memory or vector database.

memproof measures exactly these, reproducibly, with raw outputs published, and including results where zerodb loses.

## Principles

- **Prove, don't claim.** Every number comes with the command, versions, hardware and raw per-request output.
- **Neutral by construction.** Every system goes through the same adapter interface. zerodb has no special path. Anyone can add a system or rerun a result.
- **No LLM where a program can check.** Tier 1 is fully deterministic and free to run. LLM-judged quality is delegated to existing harnesses.
- **Vendor-recommended configs.** Each adapter uses the system's documented production setup, linked from the results.
- **OSS only.** Self-hosted open-source builds. No hosted clouds (several cloud ToS bar publishing benchmarks: Mem0, Zep, Redis, LangSmith, Chroma).

## What it does not do

- Rebuild LoCoMo or LongMemEval runners. Supermemory's `memorybench` (MIT) already runs them through a provider interface. We ship a zerodb provider there instead.
- Benchmark ANN index speed on static corpora. VectorDBBench already does.

## Tiers

### Tier 1: engine properties (deterministic, no LLM, runs in CI)

| Suite | What it does | Metric |
|---|---|---|
| `recall-delay` | Write a unique marker, then poll the system's own read or search API for it | Time from write ack to first successful read, p50/p99. Timeout counts as not recallable |
| `history-scale` | Grow one session from 1K to 1M records. At each size, read the last 20 and search for a known record | p50/p99 latency per size, as a curve |
| `crash` | Write with acks recorded, `kill -9` the system's server process at random points, restart, read everything | Acked writes lost (must be 0), unacked writes exposed half-written |
| `power-loss` (Linux) | Same workload on LazyFS, drop unsynced data, restart | Acked writes lost |
| `concurrent` | 2, 4, 8 processes write distinct and overlapping keys | Lost updates, duplicates, torn records |
| `forget` | Write, delete through the system's API, then query every read path | Deleted content still returned |
| `supersede` | Set a fact, update it N times, query | Stale answers returned |

Systems with no fact or update concept run `supersede` through their documented update path, or are reported as "not supported".

### Tier 2: correction recall for coding agents

Two levels.

- **Retrieval level (no LLM, CI):**
  - Corpus: 50 to 200 scripted corrections, each with a scope (file paths, task type) and later supersessions.
  - Queries: "about to edit `<path>`" or "about to do `<task>`".
  - Each system answers through its own retrieval path (its search API, `rg` over files, rules-file globs, zerodb scoped facts).
  - Metrics: precision and recall of the corrections returned, superseded corrections returned, tokens returned per query.
- **End-to-end level (LLM, pinned models, opt-in):**
  - A small fixed repo and 20 to 40 tasks.
  - A correction is given in session 1; fresh sessions touch the corrected area, unrelated areas, and areas whose correction was later replaced.
  - Metrics: repeat-mistake rate, task success, tokens injected per task.
  - Programmatic checks only (tests pass, a forbidden pattern absent). No LLM judge.

### Tier 3: recall quality (delegated)

- A zerodb provider for `memorybench`, so zerodb gets LoCoMo and LongMemEval scores with the same pipeline and judge as everyone else.
- memproof links those results. It does not re-host LoCoMo (CC BY-NC).

## Systems, first wave

| System | Why | Notes |
|---|---|---|
| Markdown files + `rg` | What developers actually use today | Headline baseline |
| Path-scoped rules files | Copilot `.instructions.md` / Cursor `.mdc` behavior | Tier 2 only |
| SQLite (WAL) | What you get for free | Tier 1 baseline |
| Mem0 OSS | Largest community | `infer=False` for Tier 1 where supported, default for Tier 2 |
| Graphiti OSS | Most cited methodology | Neo4j backend per docs |
| Chroma (self-hosted) | Common default store | |
| Qdrant | Mature, benchmark-friendly | |
| zerodb | Ours | Same adapter rules as everyone |

Letta, Cognee, LanceDB, Redis Agent Memory Server and the MCP memory server come next.

## Architecture

- Python harness (most systems ship Python SDKs). `uv` for pinned dependencies.
- One adapter per system:

```python
class Adapter(Protocol):
    name: str
    def start(self, workdir: Path) -> None: ...        # launch the system's process(es)
    def stop(self, kill: bool = False) -> None: ...     # kill=True sends SIGKILL
    def write(self, scope: Scope, record: Record) -> Ack: ...
    def read_recent(self, scope: Scope, n: int) -> list[Record]: ...
    def search(self, scope: Scope, query: str, k: int) -> list[Record]: ...
    def set_fact(self, scope: Scope, key: str, value: str) -> Ack: ...   # optional
    def delete(self, scope: Scope, ref: Ref) -> Ack: ...                 # optional
    def capabilities(self) -> set[str]: ...
```

- Each system runs in its own process or container, so `crash` kills the real server, not a Python wrapper.
- A registry maps names to adapters. Adding a system is one file plus a pinned config.
- Results:
  - raw per-operation JSONL (timestamps, latencies, pass/fail);
  - a summary table;
  - a manifest with system versions, configs, hardware, OS and harness commit.
- `memproof run --suite crash --system mem0` reproduces any single cell.

## Fairness and honesty rules

- Same machine for every system in a run. Hardware recorded.
- Pinned versions (tag or SHA) for every system and the harness.
- Vendor-recommended production config. Deviations are listed next to the result.
- Raw outputs published with every result.
- Results where zerodb loses are shown in the same tables.
- Before publishing a comparison, open an issue with each OSS project and give maintainers a chance to fix our adapter config.
- No hosted cloud products.

## Milestones

| M | Scope |
|---|---|
| B1 | Harness core, adapter protocol, results format. Adapters: markdown+`rg`, SQLite, zerodb (via its S3 server; until S3 lands, an embedded Python-side shim over the CLI). Suites: `recall-delay`, `history-scale`, `crash`, `concurrent` |
| B2 | Adapters: Mem0 OSS, Chroma, Qdrant. Suites: `forget`, `supersede` |
| B3 | Tier 2 retrieval level. Rules-files adapter |
| B4 | Graphiti adapter, Linux `power-loss` suite with LazyFS |
| B5 | Tier 2 end-to-end, `memorybench` zerodb provider |

Open-source the repo publicly at B1, with results only for the systems it covers.

## Risks

- **Self-serving perception.** Mitigations: neutral rules, open adapters, maintainers invited to fix configs, our losses shown.
- **Adapter mistakes making a competitor look bad.** Mitigation: maintainer review before publishing.
- **Tier 2 corpus bias toward zerodb's design** (path scopes). Mitigation: include task-scoped corrections that path scopes cannot express, and publish the corpus for critique.
- **zerodb not ready.** B1 can run before S3 using the shim; results are labeled pre-release.

## Open questions

1. Name: `memproof` or something else?
2. Separate GitHub org for neutrality, or under Clawdlinux?
3. License: Apache-2.0 (patent grant) or MIT?
