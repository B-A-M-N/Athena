"""Real MCP Streamable HTTP transport acceptance against a local fixture."""

from __future__ import annotations

import subprocess
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from athena.mcp.client import MCPClient
from athena.mcp.sdk_compat import make_test_server_script
from athena.protocol.errors import MCPError


def _wait_ready(url: str, process: subprocess.Popen[str]) -> None:
    # MCP 2.x imports pydantic/mcp-types lazily in the child.  On a loaded
    # release host that cold start can exceed thirty seconds; readiness still
    # has a finite bound, while the client transport timeout stays strict.
    deadline = time.monotonic() + 90
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
            if exc.code in {400, 404, 405, 406}:
                return
            time.sleep(0.1)
        except urllib.error.URLError:
            time.sleep(0.1)
    raise AssertionError("MCP Streamable HTTP fixture did not become ready")


def _proxy_canary() -> tuple[socket.socket, threading.Event, list[bool], threading.Thread]:
    """Return a local proxy listener that records any attempted connection."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.1)
    stop = threading.Event()
    hits: list[bool] = []

    def serve() -> None:
        while not stop.is_set():
            try:
                connection, _address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            hits.append(True)
            connection.close()

    thread = threading.Thread(target=serve, name="mcp-proxy-canary", daemon=True)
    thread.start()
    return listener, stop, hits, thread


@pytest.mark.dsh_release
@pytest.mark.athena_evidence("e2e")
@pytest.mark.asyncio
async def test_mcp_streamable_http_auth_discovery_call_and_reconnect(tmp_path: Path) -> None:
    pytest.importorskip("mcp", reason="the optional MCP extra is required")
    pytest.importorskip("starlette", reason="the MCP HTTP fixture needs starlette")
    pytest.importorskip("uvicorn", reason="the MCP HTTP fixture needs uvicorn")
    server = tmp_path / "fixture_mcp_http.py"
    server.write_text(make_test_server_script(), encoding="utf-8")
    # Hash-derived fixed ports collide with unrelated local services. Reserve
    # an OS-selected loopback port for this short-lived fixture instead.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    process = subprocess.Popen([sys.executable, str(server), str(port)])
    url = f"http://127.0.0.1:{port}/mcp"
    _wait_ready(url, process)
    proxy, proxy_stop, proxy_hits, proxy_thread = _proxy_canary()
    proxy_url = f"http://127.0.0.1:{proxy.getsockname()[1]}"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    monkeypatch.setenv("HTTPS_PROXY", proxy_url)
    monkeypatch.setenv("ALL_PROXY", proxy_url)
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
        monkeypatch.undo()
        proxy_stop.set()
        proxy.close()
        proxy_thread.join(timeout=1)
        await bad.close()
        await client.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)

    assert proxy_hits == [], "MCP default transport unexpectedly used an environment proxy"
