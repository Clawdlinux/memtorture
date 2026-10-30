# Agent Memory Benchmark Landscape (research for AtticDB)

Date of research: 2026-10-18. Citations are inline URLs. Items marked UNVERIFIED could not be confirmed.

## 1. Existing benchmark harnesses

### 1a. Quality/recall harnesses

| Name | License | Last activity | What it measures | Adapter interface | Gaps |
|---|---|---|---|---|---|
| LongMemEval, https://github.com/xiaowu0162/LongMemEval | MIT | 2026-05-11 | 500 QA pairs, long timestamped chats, 6 question types | None; dataset plus a scorer (`src/evaluation/evaluate_qa.py`) grading your own JSON | No latency/cost metric; LLM judge for some types; no crash/concurrency test |
| LoCoMo, https://github.com/snap-research/locomo | CC BY-NC 4.0 (non-commercial) | 2024-08-13 | 10 long two-speaker conversations, QA + summarization | None, just a JSON dataset | Non-commercial license blocks vendor use; no latency metric; small N, contested gold answers |
| BEAM, https://github.com/mohammadtavakoli78/BEAM (arXiv:2510.27246) | MIT | 2026-08-31 | 100 synthetic conversations at 128K-10M token scale, 10 memory-ability types | Authors' baseline code hardcoded to FAISS/BM25/Qdrant, no generic adapter | No third-party adapter, so vendor "BEAM scores" are not cross-verifiable; costly at 10M-token tier |
| MemoryAgentBench, https://github.com/HUST-AI-HYZ/MemoryAgentBench (arXiv:2507.05257) | MIT | 2026-09-25 | 4 competencies: Accurate Retrieval, Test-Time Learning, Long-Range Understanding, Conflict Resolution | `AgentWrapper` dispatches by string-matching `agent_name`, not a clean registry | No latency measurement; dependency conflicts between supported systems documented in README |
| MemoryArena, https://github.com/ZexueHe/MemoryArena (arXiv:2602.16313) | No license file found | 2026-06-01, "preview version" | Agentic multi-session tasks (shopping, travel, search, reasoning), reward-based success | Cleanest found: one Python class per backend under `memory/memory_systems/__init__.py`, HTTP to a local server | No license, unclear reuse rights; per-vendor API keys required; no latency/crash metric |
| Zep DMR, https://www.getzep.com/blog/state-of-the-art-agent-memory/ | n/a, one-off blog eval | — | QA recall over MemGPT paper's conversations, LLM-judge scored | None, not packaged as reusable software | Zep itself says DMR is too easy for modern LLMs and superseded it with LongMemEval |
| memorybench (Supermemory), https://github.com/supermemoryai/memorybench | MIT | 2026-10-16 | Runs LoCoMo, LongMemEval, ConvoMem through Ingest-Index-Search-Answer-Evaluate | Documented `Provider` TypeScript interface; ships adapters for supermemory, mem0, zep, filesystem, rag | Young project; judge-model cost scales with 3 datasets |
| THUIR/MemoryBench, https://github.com/THUIR/MemoryBench (arXiv:2510.17281) | MIT | 2026-09-15 | Standardized memory/continual-learning benchmark | `BaseAgent`/`BaseAgentConfig` pydantic class plus JSON configs per system | Unrelated separate project from Supermemory's "memorybench" despite similar name |
| Mem0's own eval repo, https://github.com/mem0ai/memory-benchmarks | Apache-2.0 | 2026-08-06 | Runs LoCoMo, LongMemEval, BEAM against Mem0 only | Hardcoded to Mem0 (`--backend oss|cloud` only switches deployment mode) | Cannot benchmark competitors; single-vendor lock-in |
| Letta's letta-evals, https://github.com/letta-ai/letta-evals | Apache-2.0 | 2026-08-27 | General agent eval (coding/tool tasks), not memory-recall specific | `target.kind` constrained to Letta's own runtime, needs a live server | No memory-recall dataset ships; not designed to compare Letta vs competitors |

Cross-cutting: none measure write/retrieval latency as a harness output except Mem0's and Zep's own self-reported numbers. None test crash/durability safety. Most rely on LLM-as-judge scoring, which costs money and hurts reproducibility.

### 1b. Systems-level benchmark tools

| Name | License | Last activity | Measures | Adapter mechanism | Gaps for agent memory |
|---|---|---|---|---|---|
| memtier_benchmark, https://github.com/redis/memtier_benchmark | GPL-2.0 | 2026-10-18 | Redis/Memcached throughput and latency histograms | Hardcoded C++ protocol classes, no plugin surface | No conversation concept, no vectors, no crash injection, RESP/Memcache only |
| YCSB, https://github.com/brianfrankcooper/YCSB | Apache-2.0 | 2026-08-12 | CRUD latency/throughput under workload mixes | Abstract Java class `site.ycsb.DB`; new backend is a Maven module | Flat key/value only, no vectors, no answerability, no fault injection |
| go-ycsb, https://github.com/pingcap/go-ycsb | Apache-2.0 | 2025-12-31 | Same semantics as YCSB, ~20 backends shipped | Go `DB` interface plus `RegisterDBCreator`, self-registering | Same gaps as YCSB; cleaner, lower-friction pattern to fork |
| ann-benchmarks, https://github.com/erikbern/ann-benchmarks | MIT | 2026-07-10, README says "no longer actively maintained," points to successor VIBE | Recall-vs-QPS on static HDF5 datasets | Python `BaseANN` class, per-algorithm `config.yml`, Docker per algorithm | Static corpus only, no upsert/delete, no conversation semantics, no durability |
| VectorDBBench, https://github.com/zilliztech/VectorDBBench | MIT | 2026-10-08 | Insert throughput, search QPS/recall/latency, filtered/full-text search, ~40 backends | Abstract `VectorDB`/`DBConfig`/`DBCaseConfig` classes, enum-keyed lazy-import registry | Corpus-oriented (bulk load then query), no session modeling, no answerability metric, no fault injection |
| Qdrant vector-db-benchmark, https://github.com/qdrant/vector-db-benchmark | Apache-2.0 | 2026-10-16 | Upload time, QPS, precision, p95/p99 latency under real server load | Directory-per-engine plugin layout subclassing `BaseUploader`/`BaseSearcher` | Same corpus-then-query model, no crash/fault injection despite company interest (see below) |
| Jepsen, https://github.com/jepsen-io/jepsen | Eclipse Public License v1.0 (per `project.clj`; GitHub license API returns null) | 2026-10-16 | Correctness under faults: partitions, clock skew, process kill/pause, checked via Elle/Knossos | Clojure protocols per target system (`db/DB`, `db/Process`, `db/Pause`, `client/Client`), no config-driven plug-in | Zero notion of embeddings/recall/answerability. No completed Jepsen test of any vector DB found; a Qdrant blog post (qdrant.tech/blog/qdrant-summer-of-code-24/) lists this as an open, unimplemented idea |
| LazyFS, https://github.com/dsrhaslab/lazyfs (VLDB 2024, DOI 10.14778/3681954.3681980) | MIT | 2026-08-24 | FUSE filesystem only flushing to disk on command, simulating crash/power-loss without real reboot | TOML config plus a FIFO control pipe; fault types `torn-seq`, `torn-op`, `clear` | Pure fault-injection substrate, no workload generator; used by WiredTiger's crash-consistency suite and a community Jepsen test for PowerSync (`nurturenature/jepsen-powersync`) |

Best starting skeletons: VectorDBBench's `VectorDB` ABC and Qdrant's per-engine directory convention (Python, low friction); go-ycsb's registration pattern for Go. Jepsen plus LazyFS together is the closest prior art for crash safety, but no one has built this for any agent-memory or vector database yet, a real opening for AtticDB.

## 2. Published numbers per competitor

Tags: [V]=vendor, [I]=independent. Many numbers conflict across sources; both reported where relevant.

### Mem0 (OSS)
- Write latency: GitHub issue #2813, https://github.com/mem0ai/mem0/issues/2813, confirms the "~20s add latency" claim exactly. VERIFIED. [I] Self-hosted Ollama+Qdrant reporter measured ~20s per `memory.add()` vs 3s for the chat LLM call itself; others report 2s to 20s+ depending on backend LLM. Root cause: `add()` always makes an LLM-as-judge call (docs.mem0.ai/core-concepts/memory-operations/add).
- Write-to-retrievable: platform `add()` returns `status: PENDING` plus an `event_id` to poll, no SLA given (docs.mem0.ai/platform/quickstart). [V]
- Retrieval latency: arXiv:2504.19413 Table 2, search p50 0.148s/p95 0.200s, total p50 0.708s/p95 1.440s. [V]
- Quality: paper claims LoCoMo J-score 66.88% (graph variant 68.44%); 2026 blog claims LoCoMo 92.5, LongMemEval 94.4. [V] Independent reproductions with the OSS `Memory` class got only 53.8-56% (#2800, #3943), and co-founder deshraj confirmed the paper numbers came from the SaaS platform, not OSS. [I]
- Cost: plan tiers only (Free, $19/mo, $249/mo), no $/1k-answers figure. [V]
- Durability: no explicit claim; OSS default vector store path is `/tmp`, not durable across reboots (#4279). [I]
- Failures: silent drop on embedding failure (#5245), dedup race causing permanent duplicates (#6515/#6531), concurrent writes corrupting Qdrant HNSW index (#4892), independent 32-day audit finding only 2.2% of 10,134 stored memories useful (#4573). [I]

### Zep / Graphiti
- The "162.7s" figure from the prompt is UNVERIFIED; exhaustive search found no such post. Best hypothesis: confusion with Zep's real 162ms p95 retrieval figure (getzep.com/research).
- Write/ingest: ~12s/message with OpenAI models, confirmed by a Zep maintainer in https://github.com/getzep/graphiti/issues/186. Bulk ingestion (`add_episode_bulk`) measured ~36s/record average (issue #1262). [I]
- Write-to-retrievable: help.getzep.com states "a few seconds to a few minutes"; a 100-episode import "needs minutes, not seconds." [V] Episodes can be silently dropped and never retrievable (#1707); in-memory MCP queue loses all pending episodes on restart (#1177). [I]
- Retrieval latency: getzep.com/research reports LongMemEval 90.2% accuracy at 104ms p50/162ms p95; LoCoMo 94.7% at 87ms p50/155ms p95. [V]
- Quality: arXiv:2501.13956 Table 1 DMR: Zep 94.8% vs MemGPT 93.4% vs full-context 94.4%. Table 2 LongMemEval_s: Zep 63.8% (gpt-4o-mini)/71.2% (gpt-4o). The widely repeated "Zep 63.8% vs Mem0 49.0%" comparison is flagged by an independent reviewer as false; the paper never tested Mem0 in that table. [I]
- Cost: credit-based, 1 credit/episode, Flex $125/mo for 50k credits. [V]
- Durability: hosted "Konig" graph DB claims WAL-before-ack and multi-AZ durable storage [V], but OSS Graphiti has no native durability, it passes through to Neo4j/FalkorDB/Neptune/Kuzu. Failures concentrate on the FalkorDB backend: 5 critical data-corruption issues per a maintainer meta-issue, plus 26 "duplicate" and 27 "data loss" GitHub issues found by title search. [I]

### Letta (formerly MemGPT)
- No numeric latency figures published anywhere for archival-memory insert or search. UNVERIFIED. Closest proxy: misconfigured connection pooling causing 40-60 DB queries per message, ~200-900ms overhead (issue #3219). [I]
- Quality: original MemGPT paper (arXiv:2310.08560) Table 2 DMR: 93.4% accuracy with GPT-4 Turbo. [V] 2026 blog claims 74.0% on LoCoMo via file-attachment tools (not native archival memory) vs "Mem0's reported 68.5%," a disputed cross-vendor comparison. [V]
- Cost: $0.10/active agent/month plus usage. [V]
- Durability: "state... persisted in a database, so they are never lost," unqualified, no WAL/ACID detail. [V] A competitor-disclosed static review (#3431) found the checkpoint/undo machinery exists in code but is never called in production. [I] Other failures: orphaned messages permanently corrupting conversation history (#3273), silent background persistence failures (#3151).

### Cognee
- Write latency: no vendor figure. Independent benchmark (Verging Labs "Agentic Memory Index v0.2," https://verginglabs.com/tools/cognee) measured 1.4s median time-to-store, ranked #6/12 tools, and 22.4s time-to-ready. [I]
- Retrieval: same source measured 23.9s median end-to-end answer latency, slowest of 12 tools tested. [I] PGVector backend never creates an ANN index, causing unbounded latency growth at scale (#4362). [I]
- Quality: Cognee's own eval framework reports 90.7% on LoCoMo with a gpt-5.1 judge. [V] Verging Labs independently scored it 97.1, tied #1/12. [I]
- Cost: Verging Labs measured $256.25/1,000 questions, ranked #5 cheapest of 12. [I]
- Durability: docs.cognee.ai states the default Kuzu graph backend is "one writer, one machine, no replication or failover... not suitable for concurrent use." [V] Failures: pruning with the documented safety flag set still dropped the entire relational schema (#4956); concurrent LanceDB writers produced duplicate vector IDs (#5254); unclean shutdown corrupted the WAL while the service kept reporting healthy (#4348). [I]

### MemOS
- Paper arXiv:2507.03724, repo github.com/MemTensor/MemOS (11,917 stars). Table 7 (10 QPS, GPT-4o-mini): Add mean 191.9ms/P99 376.4ms vs Mem0 (888ms/2841ms) and Zep (239ms/375.1ms). Search mean 440.5ms/P99 777.1ms vs Zep (571.7ms/5348.7ms) and Mem0 (1089ms/4637ms). [V]
- Quality: LoCoMo overall 75.80 vs Mem0 64.57, Zep 59.22; LongMemEval 77.8% vs Mem0 66.4%, Zep 63.8% (different paper versions v1/v4 show different absolutes, and a newer README claims 88.83/89.20, not reconciled). [V]
- Cost: no $/query figure, only token-count proxies.
- Durability: Neo4j+Qdrant or SQLite backend; a "MemVault" cold-storage layer described as durability-optimized, qualitative only, no RPO/RTO numbers. [V]

### Redis Agent Memory Server
- Write latency: per-session extraction ranges 2.9s (gpt-5-nano minimal) to 75.0s (gpt-5-nano high effort) per the Redis AI Research LongMemEval harness. [V]
- Write-to-retrievable: default extraction cadence is 5 minutes (configurable 60-600s); docs note the adapter "waits up to 30 minutes for extraction" and warn async failures may not surface in the API response. [V]
- Quality: "Remis + Instruct" research config scores 86.14% task-avg on LongMemEval, with caveat "do not treat 86.5% as a Redis Cloud product score"; shipping config scores 71.24%. Redis reproduced Zep/Graphiti at 71.2%, Mem0 at 49.0%, Google Vertex Memory Bank at 42.4% under its own protocol, with the caveat "some vendors publish higher scores than we report; we were not able to reproduce those numbers." [V]
- Cost: $33.25/1M conversation tokens at 86.14% accuracy (research config) vs Mastra OM's $171.65 at 84.24%; shipping config $14.47/1M tokens at 71.24%. [V]
- Durability: customer must bring own Redis instances. Docs have an explicit "Metadata Redis durability" section but no equivalent section for "Store Redis," which holds the actual memory data, a documentation gap. Default dev task backend loses pending tasks on restart; production should use the Redis-queued `docket` backend. [V]

### LangGraph store/checkpointers, Chroma, LanceDB, Qdrant, MCP memory server, markdown, SQLite
- LangGraph: no quantitative benchmark exists anywhere for Postgres/Redis/SQLite checkpointer backends. SqliteSaver is explicitly scoped by LangChain to "local development, testing, or lightweight deployments," not production. [V]
- Chroma: vendor table at 1024-dim, single node: mean query 4-8ms, p99.9 query 7-33ms, mean insert (batch 32) 112-231ms, p99.9 insert 405-1280ms, across instance types from t3.small to r7i.2xlarge. Cloud pricing: writes $2.50/logical GiB, reads $0.0075/TiB + $0.09/GiB, storage $0.33/GiB/month. [V] No independent benchmark found.
- LanceDB: Enterprise-only p50/p90/p99 table: vector search p50 25ms/p90 26ms/p99 35ms; OSS write path gives only throughput (42.3 MB/s, 8 workers), no latency. [V] No independent benchmark found.
- Qdrant: vendor single-thread benchmark: mean 1.64ms, p95 1.86ms, p99 2.01ms vs Redis 1.93ms, Weaviate 6.20ms, Milvus 13.84ms. [V] Independent benchmark from Tiger Data/Timescale (a competing pgvector vendor, bias caveat applies) at 50M x 768-dim, 99% recall: Qdrant p50 30.75ms/p95 36.73ms/p99 38.71ms vs Postgres+pgvectorscale p50 31.07ms/p95 60.42ms/p99 74.60ms, but Postgres achieves 11.4x higher throughput at that recall level. [I]
- MCP reference memory server: a flat JSONL file with an exclusive lock around read-modify-save; no performance numbers exist anywhere. Independent report (#4117) found non-atomic whole-file writes risking corruption, fixed by PR #3286.
- Markdown files as memory: no latency numbers anywhere (CLAUDE.md and the Memory Tool API). Anthropic explicitly disclaims durability: "Memory lives entirely in your application." [V]
- SQLite: core project publishes a reproducible claim of 35% faster than fread/fwrite for small blobs, and an explicit ACID/crash-test claim backed by a dedicated crash-simulation test harness (sqlite.org/transactional.html). [V] An independent 2022 study (github.com/chrisdavies/dbench) found SQLite roughly twice as fast as direct file I/O on Btrfs/Ext4 for realistic workloads. [I] `sqlite-vec`'s maintainer published SIFT1M numbers: build 4589ms/query 33ms vs faiss build 126ms/query 10ms, with an explicit caveat that this ran on a Mac Mini M1 and is not representative. [V]

### Rust memory DBs (MenteDB, Memvid, YantrikDB, Sediment, HelixDB)
All five are real, verified repositories, not hallucinated. Every benchmark number for all five is self-published with no disclosed methodology and no independent reproduction found.
- MenteDB, https://github.com/nambok/mentedb, Apache-2.0, last push 2026-10-17, 119 stars. Claims kNN ~5ms, context assembly <50ms, 0.29ms insert. [V]
- Memvid, https://github.com/memvid/memvid, Apache-2.0, last push 2026-07-14, 16,603 stars (far most popular; pivoted from an earlier Python QR-video concept to Rust, same repo). Claims "+35% SOTA on LoCoMo" and "0.025ms P50, 0.075ms P99," baseline unnamed. [V]
- YantrikDB, https://github.com/yantrikos/yantrikdb, Apache-2.0, last push 2026-10-16, 66 stars. Publishes an embedding-model comparison (MRR 0.120-0.312 across model sizes on 5,035 production memories), not latency/throughput. [V]
- Sediment, https://github.com/rendro/sediment, MIT, last push 2026-03-30 (quiet ~6 months), 40 stars. Self-reported vs ChromaDB/Mem0/MCP Memory Service, no disclosed methodology: store p50 50ms (vs ChromaDB 692ms, Mem0 14ms, MCP 2ms, i.e. not fastest on raw store latency despite claiming best recall/recency/dedup quality). [V]
- HelixDB, https://github.com/HelixDB/helix-db, Apache-2.0, last push 2026-10-18, 6,211 stars, oldest and most mature. General graph+vector+full-text DB, not narrowly an agent-memory product. No benchmark numbers found in README. Multiple unrelated namesake repos exist; confirm you are looking at HelixDB/helix-db.

## 3. Gaps nobody publishes today

Dimensions users complain about in GitHub issues but that no vendor or harness reports as a number:
1. Acked-write survival under `kill -9` or power loss. No vendor states an RPO/RTO. No harness (Jepsen aside, unrun on these systems) tests this. AtticDB's durability design is directly relevant here.
2. Latency versus history length (does add/search slow down at 10k, 100k, 1M memories). Only Cognee's PGVector full-table-scan issue (#4362) hints at this; no one publishes a curve.
3. Concurrent multi-process writers losing updates. Real bugs reported for Mem0 (#4892), Cognee (#5254), Zep's FalkorDB backend, never measured as a benchmark metric.
4. Read-your-writes / answerability delay, time from write-ack to the fact being retrievable. Documented qualitatively by Zep ("minutes, not seconds") and Redis (5-30 minute window) but never reported as a hard percentile. AtticDB's read-your-writes design is a genuine differentiator to quantify.
5. Forget completeness: does delete remove all copies (vector index, graph edges, cache). Cognee's #4956 and Mem0's dedup bugs suggest this is broken in practice; unbenchmarked.
6. Stale-fact rate after updates/supersession. No system publishes a metric for how often it answers with an outdated fact.
7. Cost per operation including LLM calls, end to end. Only Verging Labs (independent) and Redis (vendor) publish a $/1k-answers or $/1M-token figure; others give plan-tier pricing only.
8. Behavior at 1M+ memories. BEAM's 10M-token tier is the closest public attempt but costly to run, and only Mem0's own eval repo reports BEAM(10M) numbers (48.6), with no competitor cross-run at that scale.

## 4. Proposed benchmark suite design

### Dimensions and workloads
1. Ingest latency: per-memory add latency, p50/p95/p99, at fixed concurrency (1, 8, 64 writers), measured separately from any LLM-extraction step so embedding/LLM time and storage-engine time are not conflated.
2. Answerability delay: time from write-ack to first successful retrieval of that fact via the system's own search/query API, not a side-channel poll. Targets gap #4 and is AtticDB's strongest claimed advantage (read-your-writes).
3. Retrieval latency at increasing corpus size: p50/p99 at 1k, 10k, 100k, 1M memories, same query mix, to expose the latency-vs-history-length curve (gap #2).
4. Crash safety: Jepsen-style or LazyFS-style fault injection, kill -9 and simulated power loss right after write-ack, verify no acked write is lost and no un-acked write is silently exposed. The single biggest unaddressed gap in the landscape (section 3, item 1) and plays to AtticDB's durable append-only log.
5. Concurrent writer correctness: N processes writing distinct and overlapping keys simultaneously, check for lost updates, duplicate entries, or torn writes (gap #3).
6. Forget completeness and stale-fact rate: issue a forget/delete or an update-supersedes-old-fact, then query both the primary index and any secondary index (graph, cache) to confirm the fact is actually gone or updated everywhere (gaps #5, #6).
7. Quality/recall: run LongMemEval and LoCoMo (LoCoMo's CC BY-NC license blocks commercial redistribution of the dataset itself, so publish scores but not re-host the data) through a shared ingest-then-QA pipeline, same judge model and prompt for every system.
8. Cost per 1k answers: track wall-clock LLM token spend (extraction + judge + generation) end to end, not just storage cost (gap #7).
9. Scale behavior: repeat 1-6 at 1M+ memories for at least the top few systems, budget permitting (gap #8).

### Fairness rules
- Same hardware for every system, documented (CPU, RAM, disk type, network).
- Same embedding model and same LLM (extraction and judge) across systems that need one; document any system that cannot swap embedding model and benchmark it with its own default as a clearly labeled exception.
- Pin exact versions (commit SHA or release tag) of every system and harness component; publish the pin list.
- Use each vendor's own documented recommended production configuration, not a deliberately weak default, and link to the config used.
- Publish raw per-request output (latencies, timestamps, pass/fail), not just aggregates, so results are independently re-checked.
- Avoid LLM-as-judge where a programmatic check suffices (exact match, membership, numeric diff); use it only for open-ended QA grading, and publish the judge prompt and model version.
- Disclose any case where a vendor's cloud ToS restricts benchmark publication (section 5) and whether the run was against OSS, self-hosted, or managed cloud.

### Adapter interface shape
Recommend a Python harness. Most competitors ship a Python SDK or are themselves Python (Mem0 OSS, Cognee, Letta's client, Graphiti, LangGraph), and the two most mature adapter patterns found (VectorDBBench's `VectorDB` ABC and supermemory/memorybench's `Provider` interface) are Python/TypeScript, not Go or Clojure. A thin Rust client (PyO3 bindings or a local HTTP sidecar) lets AtticDB plug in without forcing the suite into Rust.

Minimal interface, modeled on VectorDBBench's ABC and supermemory's Provider:
```python
class MemoryAdapter(ABC):
    def init(self, config: dict) -> None: ...
    def add(self, text: str, metadata: dict) -> AddResult:  # timed; returns ack_time, memory_id
    def search(self, query: str, k: int) -> list[SearchResult]: ...  # timed
    def forget(self, memory_id: str) -> None: ...
    def flush(self) -> None: ...  # before crash-injection points
    def close(self) -> None: ...
```
A registry (name to adapter class) mirrors go-ycsb's `RegisterDBCreator` pattern. Crash-safety tests drive the adapter's process directly, not through the Python wrapper, so the kill/restart cycle is realistic, similar to how Jepsen separates its client protocol from its db/Process protocol.


### Systems to include first
Top 5 self-hostable systems by usage, in priority order: Mem0 OSS (largest community), Zep/Graphiti OSS (most rigorous published methodology to match against), Qdrant (mature vector DB with an existing benchmark culture, lowest ToS risk), Chroma (popular embedded/self-hosted default), and plain SQLite as the baseline every reader understands. Add Letta next if time allows (clean ToS, Apache-2.0, interesting agent-native design).

### Baselines
- Markdown-file baseline: a flat directory of dated markdown files, append-only, read via plain file I/O and a simple grep or re-embed-on-query search. Sets a floor for latency and a ceiling for simplicity, with zero published numbers anywhere today.
- SQLite baseline: a single table with `created_at`, `text`, and an optional `sqlite-vec` embedding column, WAL mode on, representing "what you get for free with no agent-memory product at all." SQLite's own crash-test harness and dbench numbers (section 2) give us something to sanity-check our own crash-safety test against.

### Where AtticDB would likely lose honestly
- Semantic/vector recall quality before it ships search: on LongMemEval/LoCoMo-style QA, systems with graph extraction (Zep/Graphiti, MemOS) or LLM-based fact extraction (Mem0, Cognee) will likely beat a pure append-only log with recency-window reads on multi-hop and knowledge-update question types, which require reasoning over relationships, not just recency.
- Ecosystem maturity: most competitors have existing LangChain/LlamaIndex integrations and larger community-tested backend options; AtticDB's adapter will be new and untested here.
- Benchmarks rewarding LLM-extraction-based dedup/consolidation (MemOS, Cognee) over raw append-only storage will favor those systems on "stale-fact rate" unless AtticDB's supersession feature is complete and tested.
Report these honestly in the same raw-output format as our wins, per the fairness rules above.

## 5. Licensing and ToS risk of publishing comparative benchmarks

Every OSS library in this set is permissively licensed (Apache-2.0 or MIT) with no benchmark-restriction clause in any LICENSE file. All risk is in commercial cloud Terms of Service, where a DeWitt-clause pattern was found in several contracts (research run 2026-10-18; this category-specific finding does not appear in existing public DeWitt-clause writeups such as https://danluu.com/anon-benchmark/ or https://cube.dev/blog/dewitt-clause-or-can-you-benchmark-a-database, which only cover Oracle, Microsoft SQL Server, Salesforce, and Databricks/Snowflake).

| Product | OSS license | DeWitt-style clause in cloud ToS | Risk for benchmarking hosted/cloud product |
|---|---|---|---|
| Mem0 | Apache-2.0 | Yes, no carve-out. mem0.ai/terms §2.3(i) bars "competitive analysis or benchmarking" outright | High |
| Zep/Graphiti | Apache-2.0 | Yes, internal-use carve-out. getzep.com/legal/terms/ §2.3 bars public disclosure of benchmarks but permits internal testing | High for publishing; low for internal testing |
| Letta | Apache-2.0 | None found | Low |
| Cognee | Apache-2.0 | None found | Low |
| MemOS | Apache-2.0 | Cloud ToS unverifiable, JS-rendered SPA | Unknown, recommend manual check |
| Redis (Cloud/Software) | Tri-licensed RSALv2/SSPLv1/AGPLv3 | Yes, explicit, two contracts (Cloud ToS §2.2(d), Software Agreement §2.2(f)), bar benchmark testing and disclosure without approval | High for Cloud/Enterprise; low for self-hosted OSS core |
| LangGraph/LangSmith Platform | MIT (library) | Yes. langchain.com/terms-of-service §2.4(c) bars publishing benchmarks of the Platform | High for Platform accounts; low for the library |
| Chroma | Apache-2.0 | Yes, broadest wording: trychroma.com/terms bars publishing "benchmarks or performance information about the Service" | High for Chroma Cloud; low for self-hosted |
| LanceDB | Apache-2.0 | None found | Low |
| Qdrant | Apache-2.0 | None found; Qdrant itself publishes comparative benchmarks against competitors | Low |

Key caveat: every "High" risk binds only parties who created an account on that vendor's paid cloud service; it does not restrict benchmarking the underlying Apache-2.0/MIT code run independently. Benchmark the OSS/self-hosted form of Mem0, Zep/Graphiti, Redis, LangGraph, and Chroma (low risk in that mode), and avoid their hosted cloud products without written permission. Confirm scope with counsel before publishing anything touching a flagged hosted product.

## Summary of biggest uncertainties
- "Zep 162.7s" could not be found anywhere; likely a garbled retelling of Zep's real 162ms p95 figure. UNVERIFIED.
- Mem0's "~20s add latency" is fully verified at GitHub issue #2813.
- MemOS Cloud ToS could not be checked (JS-only site).
- No completed Jepsen-style crash-safety test exists for any vector or agent-memory database as of this research; the clearest opportunity for AtticDB to publish genuinely new numbers.
