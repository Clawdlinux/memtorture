Sponsored and built by the Clawdlinux team, makers of AtticDB (formerly zerodb), an agent memory engine. AtticDB will be tested here from milestone B4, under the same rules as every other system.

# memtorture

Torture tests for agent memory engines. Evidence, not marketing.

B1 measures recall delay, history growth, process recovery, concurrent append, and concurrent keyed writes.
B2 adds write throughput, keyed supersession, delete visibility, plaintext residue, and clean-open recovery.
Process kills test user-space buffering and recovery, not disk durability.

## Run

Python 3.12 and [uv](https://docs.astral.sh/uv/) are required.

```sh
uv sync --locked
uv run memtorture controls
uv run memtorture run --suite process-kill --system sqlite --config synchronous=FULL
uv run memtorture report --local
```

Run full controls before a local benchmark:

```sh
uv run memtorture controls
uv run memtorture run --all
```

The quick check uses 5 trials and never authorizes benchmark cells.
The full gate uses 20.
Evidence lives under `results/controls/<os>-<arch>/`, with a separate gate for each platform.
Gated cells refuse to run if full controls are missing, stale, or not discriminating.
History scale, write throughput, and clean-open recovery are ungated measurement suites.
The approved spec defines no fault controls for them.
Unsupported adapters show N/A with a reason.
Local results are not cleared for comparative publication.

See [methodology](docs/METHODOLOGY.md) for measurements and limits.

## Worker protocol

Workers can use any language. The harness validates every protocol v1 reply.
Commands come from `systems/<name>/manifest.json`.
See [the protocol and conformance kit](docs/PROTOCOL.md) for schemas and wire examples.

The standalone Node reference needs no packages:

```sh
uv run memtorture conformance --command "node systems/reference-node/worker.mjs"
```

Cross-language tests find Node on PATH and skip when it is missing.
CI installs Node 22 so those tests run.
Conformance failures keep `.conformance-work/` logs and print a stderr tail.
Only the newest 10 failed runs are retained. Active runs are preserved.

## External adapters

Node 22 is needed for the reference MCP server.
Qdrant runs as a native binary.

```sh
uv run python -m scripts.prepare_mcp
uv run python -m scripts.fetch_qdrant
```

The MCP preparation script scans pinned sources before installation.
Only complete `SAFE` verdicts permit execution.
Blocked scans disable the adapter.
The Qdrant script checks a committed SHA-256 before extracting the binary.
See [security notes](docs/SECURITY.md) for pins and scan rules.

Use exact config rows:

```sh
uv run memtorture run --suite recall-delay --system qdrant --config wait=true
uv run memtorture run --suite history-scale --system markdown --config append
```

`wait=true` controls visibility, not fsync.
History grows to 100K records by default.
`--extended` adds 1M for history and recovery. It also raises throughput writes per writer from 20 to 100.

## Governance

Only control results and methodology are committed.
Run output goes only under `results/runs/`, which is gitignored.
Control evidence goes only under `results/controls/<os>-<arch>/`.
The harness rejects any other output path.
Governance blocks comparative results, so the harness writes no multi-system tables.
`memtorture report --local` prints them to your terminal with a LOCAL ONLY banner.
`memtorture export-system --system <name>` writes one system's raw output for its upstream issue.
It lands under `results/runs/exports/` and carries the harness commit and platform.
CI runs controls and baseline systems only (markdown, SQLite).
An outside maintainer must approve methodology before comparative publication.
Each upstream project gets a 14-day reply window.
Config fixes are welcome, including fixes that change the sponsor's standing.
Published numbers must include raw operations, pinned versions, and machine details.
Sponsor marketing must link the full run.

## Dependencies

The harness has no runtime dependencies. It uses only the Python standard library.
Qdrant is driven through its native REST API with `http.client` over one keep-alive connection.
A new runtime dependency needs a written reason in its pull request.
The test tools are pytest, Hypothesis, and Ruff.

Code uses Apache-2.0.
Corpus and results use CC BY 4.0.
