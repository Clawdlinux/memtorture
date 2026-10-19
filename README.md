Sponsored and built by the Clawdlinux team, makers of zerodb, an agent memory engine. zerodb will be tested here from milestone B4, under the same rules as every other system.

# memproof

Agent memory benchmarks. Evidence, not proof.

B1 measures recall delay, history growth, process recovery, concurrent append, and concurrent keyed writes.
Process kills test user-space buffering and recovery, not disk durability.

## Run

Python 3.12 and [uv](https://docs.astral.sh/uv/) are required.

```sh
uv sync --locked
uv run memproof controls --quick
uv run memproof run --suite process-kill --system sqlite --config synchronous=FULL
uv run memproof report
```

Run full controls before a local benchmark:

```sh
uv run memproof controls
uv run memproof run --all
```

The quick gate uses 5 trials.
The full gate uses 20.
Cells refuse to run if controls are missing, stale, or not discriminating.
Unsupported adapters show N/A with a reason.
Local results are not cleared for comparative publication.

See [methodology](docs/METHODOLOGY.md) for measurements and limits.

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
uv run memproof run --suite recall-delay --system qdrant --config wait=true
uv run memproof run --suite history-scale --system markdown --config append
```

`wait=true` controls visibility, not fsync.
History grows to 100K records by default.
`--extended` adds 1M.

## Governance

Only control results and methodology are committed.
Comparative outputs stay local under `results/`.
An outside maintainer must approve methodology before comparative publication.
Each upstream project gets a 14-day reply window.
Config fixes are welcome, including fixes that change the sponsor's standing.
Published numbers must include raw operations, pinned versions, and machine details.
Sponsor marketing must link the full run.

## Dependencies

`mcp` is reserved for the gated reference-server adapter.
`qdrant-client` speaks the native Qdrant API.
The test tools are pytest, Hypothesis, and Ruff.

Code uses Apache-2.0.
Corpus and results use CC BY 4.0.
