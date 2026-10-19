Sponsored and built by the Clawdlinux team.

# memproof

Agent memory benchmarks. Evidence, not proof.

B1 measures recall delay, history growth, process recovery, concurrent append, and concurrent keyed writes.
Process kills test user-space buffering and recovery, not disk durability.

## Run

Python 3.12 and [uv](https://docs.astral.sh/uv/) are required.

```sh
uv sync --locked
uv run memproof controls --quick
# Available after the B1 harness is complete:
uv run memproof run --suite process-kill --system sqlite --config synchronous=FULL
```

The initial scaffold has no working control gate.
It cannot publish benchmark cells.

## Governance

Only control results and methodology are committed.
Comparative outputs stay local under `results/`.
An outside maintainer must approve methodology before comparative publication.
Each upstream project gets a 14-day reply window.
Config fixes are welcome, including fixes that change the sponsor's standing.
Published numbers must include raw operations, pinned versions, and machine details.
Sponsor marketing must link the full run.

## Dependencies

`mcp` speaks the reference server's stdio protocol.
`qdrant-client` speaks the native Qdrant API.
The test tools are pytest, Hypothesis, and Ruff.

Code uses Apache-2.0.
Corpus and results use CC BY 4.0.
