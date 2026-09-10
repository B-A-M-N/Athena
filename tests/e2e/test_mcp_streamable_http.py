"""Real MCP Streamable HTTP transport acceptance against a local fixture."""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from athena.mcp.client import MCPClient
from athena.protocol.errors import MCPError


def _wait_ready(url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"MCP fixture exited with {process.returncode}")
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers={"Authorization": "Bearer secret"}),
                timeout=1,
            ):
                return
        except urllib.error.HTTPError as exc:
            if exc.code in {400, 405}:
                return
            time.sleep(0.1)
        except urllib.error.URLError:
            time.sleep(0.1)
    raise AssertionError("MCP Streamable HTTP fixture did not become ready")


@pytest.mark.dsh_release
@pytest.mark.athena_evidence("e2e")
@pytest.mark.asyncio
async def test_mcp_streamable_http_auth_discovery_call_and_reconnect(tmp_path: Path) -> None:
    pytest.importorskip("mcp", reason="the optional MCP extra is required")
    pytest.importorskip("starlette", reason="the MCP HTTP fixture needs starlette")
    pytest.importorskip("uvicorn", reason="the MCP HTTP fixture needs uvicorn")
    server = tmp_path / "fixture_mcp_http.py"
    server.write_text(
        """
import sys
from contextlib import asynccontextmanager

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

port = int(sys.argv[1])
mcp = FastMCP('athena-http-fixture', stateless_http=True)

@mcp.tool()
def add(left: int, right: int) -> int:
    return left + right

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.headers.get('authorization') != 'Bearer secret':
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        return await call_next(request)

@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield

app = Starlette(routes=[
    __import__('starlette.routing', fromlist=['Mount']).Mount('/mcp', app=mcp.streamable_http_app())
], lifespan=lifespan)
app.add_middleware(AuthMiddleware)
uvicorn.run(app, host='127.0.0.1', port=port, log_level='error')
        """.lstrip(),
        encoding="utf-8",
    )
    port = 18_000 + (abs(hash(str(tmp_path))) % 1_000)
    process = subprocess.Popen([sys.executable, str(server), str(port)])
    url = f"http://127.0.0.1:{port}/mcp"
    _wait_ready(url, process)
    client = MCPClient(
        "fixture-http",
        url=url,
        headers={"Authorization": "Bearer secret"},
        connect_timeout=10,
    )
    bad = MCPClient(
        "fixture-http-bad-auth",
        url=url,
        headers={"Authorization": "Bearer wrong"},
        connect_timeout=3,
    )
    try:
        with pytest.raises(MCPError):
            await bad.connect()
        await client.connect()
        tools = await client.list_tools()
        assert [tool.name for tool in tools] == ["add"]
        result = await client.call_tool("add", {"left": 2, "right": 3})
        assert result.is_error is False
        assert "5" in result.content

        process.terminate()
        process.wait(timeout=10)
        with pytest.raises(MCPError):
            await client.list_tools()

        process = subprocess.Popen([sys.executable, str(server), str(port)])
        _wait_ready(url, process)
        await client.close()
        await client.connect()
        result = await client.call_tool("add", {"left": 7, "right": 8})
        assert result.is_error is False
        assert "15" in result.content
    finally:
        await bad.close()
        await client.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
