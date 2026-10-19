# External processes

No installer is piped into a shell.
All downloaded sources stay outside Git.

## MCP memory server

The pin is `@modelcontextprotocol/server-memory@2.0.0`.
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
There is no approval bypass in the script.
Changed sources need a new scan.
The current adapter is a disabled stub.
It does not execute Node.

### Why 2.0.0

The earlier pin `2025.11.25` used SDK 1.x.
That tree had 96 packages and 3630 files, including express, hono, jose and OAuth code.
A stdio memory server needs none of them.
2.0.0 uses the split SDK 2.x.
Its tree is 4 packages and 950 files: `server-memory`, `@modelcontextprotocol/server`, `@modelcontextprotocol/core` and `zod`.
None has an install script.

2.0.0 also changes storage behavior.
Writes take a lock file and replace the graph with an atomic rename.
That fixes servers #1819 and #4797.
Testing the older pin would measure bugs upstream already fixed.
2.0.0 does not call `fsync`, so power loss can still drop acked writes.

2.0.0 was published on 2026-10-19.
We do not run a release for published numbers until it is at least 7 days old.

### Scan results for 2.0.0

| Target | Verdict | HIGH | Fully inspected |
|---|---|---:|---|
| `server-memory@2.0.0` | `CAUTION` | 0 | 3 of 4 files |
| `@modelcontextprotocol/server@2.3.1` | `DO_NOT_INSTALL` | 43 | 60 of 85 files |
| `@modelcontextprotocol/core@2.3.1` | `DO_NOT_INSTALL` | 11 | 21 of 21 files |
| `zod@4.6.7` | `DO_NOT_INSTALL` | 15 | 0 of 840 files |
| Whole tree | `DO_NOT_INSTALL` | 60 | 0 of 950 files |

The whole-tree row is a separate scan run over the same 950 files.
Its totals differ from the sum of the per-package runs, which is 69 HIGH and 84 files fully inspected.
We report both as SkillSpector printed them.

The HIGH findings we sampled are pattern hits, not behavior:
- credential-access and prompt-extraction hits on protocol names such as `PromptArgumentSchema` and on OAuth type definitions;
- a YARA `exploit_framework` hit on zod's `keyof`;
- hidden-instruction hits on zod's Kannada and Persian error strings;
- partial coverage on the ajv validator bundled inside `@modelcontextprotocol/server`.

These are scanner findings, not a claim of malicious upstream code.
We did not override them.
No Node server was executed.
Raw reports stay under `.tools/mcp/`.
The committed summary is `scripts/mcp/scan-verdict.json`.
The preparation script refuses known blocked pins before running npm.

### Paths to enable it

1. A SkillSpector run with LLM analysis on these 4 public packages.
   It sends only public npm source to the LLM provider.
   It needs the owner's approval.
   The result must be a complete `SAFE`.
2. Then run the server under the Node permission model.
   Reads are limited to the package directory and the data directory.
   Writes are limited to the data directory.
   Child processes, workers and native addons are denied.
   Network is not needed by a stdio server.

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
