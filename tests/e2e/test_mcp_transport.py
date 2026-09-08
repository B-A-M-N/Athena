"""Real MCP stdio transport acceptance against a local fixture server."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from athena.mcp.client import MCPClient


@pytest.mark.dsh_release
@pytest.mark.athena_evidence("e2e")
@pytest.mark.asyncio
async def test_mcp_stdio_transport_discovers_calls_and_reconnects(tmp_path: Path) -> None:
    """Exercise the actual SDK transport and a separate MCP server process."""
    pytest.importorskip("mcp", reason="the optional MCP extra is required")
    server = tmp_path / "fixture_mcp_server.py"
    server.write_text(
        """
from mcp.server.mcpserver import MCPServer

server = MCPServer('athena-fixture')

@server.tool()
def add(left: int, right: int) -> int:
    return left + right

if __name__ == '__main__':
    server.run(transport='stdio')
""".lstrip(),
        encoding="utf-8",
    )

    client = MCPClient(
        "fixture-stdio",
        command=sys.executable,
        args=[str(server)],
        cwd=str(tmp_path),
        # The stdio server is a real child process; allow cold Python/MCP
        # imports to complete while retaining a hard upper bound.
        connect_timeout=30,
    )
    try:
        await client.connect()
        tools = await client.list_tools()
        assert [tool.name for tool in tools] == ["add"]
        result = await client.call_tool("add", {"left": 2, "right": 3})
        assert result.is_error is False
        assert "5" in result.content

        await client.close()
        await client.connect()
        result = await client.call_tool("add", {"left": 7, "right": 8})
        assert result.is_error is False
        assert "15" in result.content
    finally:
        await client.close()
