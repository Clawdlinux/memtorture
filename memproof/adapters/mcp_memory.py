from memproof.adapters.base import Base
from memproof.types import NotSupported

VERSION = "2.0.0"
BLOCK_REASON = (
    f"MCP memory {VERSION} is disabled. Package source: CAUTION, 3/4 files. "
    "Dependency scan: DO_NOT_INSTALL, score 100, incomplete coverage. "
    "0/950 dependency files fully inspected. "
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
