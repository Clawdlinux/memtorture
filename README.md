Sponsored and built by the Clawdlinux team, makers of zerodb, an agent memory engine. zerodb will be tested here from milestone B4, under the same rules as every other system.

# memtorture

Torture tests for agent memory engines. Evidence, not marketing.

B1 measures recall delay, history growth, process recovery, concurrent append, and concurrent keyed writes.
Process kills test user-space buffering and recovery, not disk durability.

## Run

Python 3.12 and [uv](https://docs.astral.sh/uv/) are required.

```sh
uv sync --locked
uv run memtorture controls
uv run memtorture run --suite process-kill --system sqlite --config synchronous=FULL
uv run memtorture report
```

Run full controls before a local benchmark:

```sh
uv run memtorture controls
uv run memtorture run --all
```

The quick check uses 5 trials and never authorizes benchmark cells.
The full gate uses 20.
Gated cells refuse to run if full controls are missing, stale, or not discriminating.
History scale is an ungated measurement suite.
The approved spec defines no control for it.
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
uv run memtorture run --suite recall-delay --system qdrant --config wait=true
uv run memtorture run --suite history-scale --system markdown --config append
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

The harness has no runtime dependencies. It uses only the Python standard library.
Qdrant is driven through its native REST API with `http.client` over one keep-alive connection.
A new runtime dependency needs a written reason in its pull request.
The test tools are pytest, Hypothesis, and Ruff.

Code uses Apache-2.0.
Corpus and results use CC BY 4.0.
