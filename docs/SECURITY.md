# External processes

No installer is piped into a shell.
All downloaded sources stay outside Git.

## MCP memory server

The pin is `@modelcontextprotocol/server-memory@2025.11.25`.
`scripts/mcp/package-lock.json` pins its dependency tree.
The tarball SHA-512 is fixed in `scripts/prepare_mcp.py`.

```sh
uv run python -m scripts.prepare_mcp
```

The script packs the exact version without lifecycle scripts.
It checks integrity before extraction.
SkillSpector inspects the extracted sources with LLM analysis disabled.
Only a complete `SAFE` verdict permits installation.
Dependencies install with lifecycle scripts disabled.
The installed tree gets a second scan before execution.

`CAUTION`, `DO_NOT_INSTALL`, scan failures, and incomplete coverage disable the adapter.
The adapter raises `NotSupported` with the reason.
There is no approval bypass.
Changed sources need a new scan.
The current adapter is a disabled stub.
It does not execute Node.

Source reports and attestations stay under `.tools/mcp/`.
The source scan inspected all 3 package files.
It returned `SAFE` with a risk score of 18.
Findings included unpinned examples and an upstream SDK version range.
We do not run those examples.
The checked-in lockfile fixes the SDK version.

The installed dependency tree did not pass.
SkillSpector returned `DO_NOT_INSTALL`, score 100, severity `CRITICAL`.
All 3630 components had partial coverage.
No component was fully inspected.
Findings included credential-access indicators, tool misuse, YARA matches, and SSRF indicators.
Obfuscated instruction text prevented complete deterministic inspection.
These are scanner findings, not a claim of malicious upstream code.
We did not override them.

No Node server was executed.
The blocked installed tree was removed.
Raw reports stay under `.tools/mcp/`.
The committed summary is `scripts/mcp/scan-verdict.json`.
Re-enabling this adapter requires a complete safe scan and a new implementation review.

## Qdrant

The pin is `1.16.3`.
Archive digests come from the pinned GitHub release metadata.
`scripts/qdrant.sha256` contains macOS arm64 and Linux x86_64 digests.

```sh
uv run python -m scripts.fetch_qdrant
```

The script checks SHA-256 before extracting the binary.
The adapter rejects a changed binary.
Qdrant binds to loopback.
Each server gets a private API key.
Telemetry is disabled.
Servers use native processes and isolated data directories.

`wait=false` and `wait=true` describe applied visibility, not fsync.
[The v1.16 API](https://api.qdrant.tech/v-1-16-x/api-reference/points/upsert-points)
says: "If true, wait for changes to actually happen".
