"""MCP integration subsystem (§90-92).

MCP servers are tool/data sources, not agents (INV-001). Tools become
capabilities via :class:`~athena.mcp.adapter.MCPAdapter`; resources become
context content via :class:`~athena.mcp.resources.MCPResourceProvider`.

No ``mcp`` SDK import happens at module import time; it is imported lazily
inside :class:`~athena.mcp.client.MCPClient`.
"""

from __future__ import annotations

from athena.mcp.client import (
    MCPClient,
    MCPPromptRef,
    MCPResourceRef,
    MCPToolRef,
    MCPToolResult,
)
from athena.mcp.adapter import MCPAdapter, MCPToolExecutor
from athena.mcp.resources import MCPResourceProvider, mcp_provenance
from athena.mcp.tools import (
    canonical_capability_id,
    friendly_alias,
    infer_effects,
    sanitize_server_name,
    tool_schema_to_descriptor_input,
)

__all__ = [
    "MCPClient",
    "MCPToolRef",
    "MCPResourceRef",
    "MCPPromptRef",
    "MCPToolResult",
    "MCPAdapter",
    "MCPToolExecutor",
    "MCPResourceProvider",
    "mcp_provenance",
    "canonical_capability_id",
    "friendly_alias",
    "sanitize_server_name",
    "infer_effects",
    "tool_schema_to_descriptor_input",
]
