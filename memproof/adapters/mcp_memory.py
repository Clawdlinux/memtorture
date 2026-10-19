from memproof.adapters.base import Base
from memproof.types import NotSupported

VERSION = "2025.11.25"
BLOCK_REASON = (
    "MCP memory 2025.11.25 is disabled. Package source: SAFE, 3/3 files. "
    "Dependency scan: DO_NOT_INSTALL, score 100, incomplete coverage. "
    "0/3630 dependency components fully inspected. "
    "See docs/SECURITY.md and scripts/mcp/scan-verdict.json. "
    "No Node server was executed."
)


class MCPMemory(Base):
    """Security-gated stub. Do not execute the blocked dependency tree."""

    name = "mcp-memory"
    version = VERSION
    notes = BLOCK_REASON

    def capabilities(self):
        return set()

    def start(self, workdir, config):
        raise NotSupported(BLOCK_REASON)
