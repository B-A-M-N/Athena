"""Version-gated compatibility boundary for the optional MCP SDK.

Athena supports MCP SDK 2.x for beta.  The module still detects the older v1
surface so an unsupported installation fails with a useful readiness error,
instead of failing later on a version-specific import.  All SDK transport and
session construction belongs here; the client consumes only these normalized
helpers.
"""

from __future__ import annotations

import importlib
import inspect
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from typing import Any, Mapping

from athena.protocol.errors import MCPError


@dataclass(frozen=True)
class MCPSDKCompatibility:
    major: int
    streamable_http_client: Any
    stdio_client: Any
    stdio_parameters: Any
    client_session: Any
    http_client_parameter: str | None
    http_client_module: Any | None
    server_factory: Any | None

    @property
    def supported(self) -> bool:
        return self.major == 2

    @property
    def readiness_error(self) -> str | None:
        if self.supported:
            return None
        return (
            f"installed MCP SDK major {self.major} is not supported by this beta; "
            "install the locked MCP 2.x extra"
        )


def _major_version(module: Any) -> int:
    # MCP 2.x deliberately does not expose __version__ at the package root.
    try:
        metadata = importlib.import_module("importlib.metadata")
        raw = metadata.version("mcp")
        return int(str(raw).split(".", 1)[0])
    except (PackageNotFoundError, ValueError, TypeError):
        # A test double or vendored SDK may omit package metadata.  The v2
        # server module is an unambiguous capability marker.
        try:
            importlib.import_module("mcp.server.mcpserver")
        except ImportError:
            return 1
        return 2


def load_sdk() -> MCPSDKCompatibility:
    try:
        root = importlib.import_module("mcp")
        http_mod = importlib.import_module("mcp.client.streamable_http")
        stdio_mod = importlib.import_module("mcp.client.stdio")
        major = _major_version(root)
        if major >= 2:
            streamable = http_mod.streamable_http_client
            http_parameter: str | None = "http_client"
            http_module = importlib.import_module("httpx2")
            server_factory = importlib.import_module("mcp.server.mcpserver").MCPServer
        else:
            # v1 used the old spelling and FastMCP fixture.  It remains
            # detectable for diagnostics, but beta packaging rejects it.
            streamable = http_mod.streamablehttp_client
            http_parameter = (
                "httpx_client_factory"
                if "httpx_client_factory" in inspect.signature(streamable).parameters
                else None
            )
            http_module = importlib.import_module("httpx")
            server_factory = getattr(importlib.import_module("mcp.server.fastmcp"), "FastMCP", None)
        return MCPSDKCompatibility(
            major=major,
            streamable_http_client=streamable,
            stdio_client=stdio_mod.stdio_client,
            stdio_parameters=stdio_mod.StdioServerParameters,
            client_session=root.ClientSession,
            http_client_parameter=http_parameter,
            http_client_module=http_module,
            server_factory=server_factory,
        )
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        raise MCPError(
            "the installed 'mcp' package does not expose a supported SDK transport; "
            "install the locked MCP 2.x extra"
        ) from exc


def require_supported_sdk() -> MCPSDKCompatibility:
    sdk = load_sdk()
    if not sdk.supported:
        raise MCPError(sdk.readiness_error or "unsupported MCP SDK")
    return sdk


def make_http_client(
    sdk: MCPSDKCompatibility,
    *,
    headers: Mapping[str, str] | None,
    timeout: float,
    trust_env: bool,
) -> Any:
    """Build the SDK-native HTTP client with an explicit proxy policy."""
    if sdk.http_client_module is None or sdk.http_client_parameter is None:
        raise MCPError("MCP SDK cannot expose a controllable HTTP client")
    client_type = getattr(sdk.http_client_module, "AsyncClient", None)
    if client_type is None:
        raise MCPError("MCP SDK HTTP client dependency is unavailable")
    return client_type(
        headers=dict(headers or {}),
        timeout=float(timeout),
        trust_env=bool(trust_env),
    )


def streamable_http_context(
    sdk: MCPSDKCompatibility,
    url: str,
    *,
    headers: Mapping[str, str] | None,
    timeout: float,
    trust_env: bool,
) -> AbstractAsyncContextManager[Any]:
    if sdk.http_client_parameter is None:
        raise MCPError("MCP SDK cannot prove its HTTP proxy policy")
    client = make_http_client(
        sdk,
        headers=headers,
        timeout=timeout,
        trust_env=trust_env,
    )
    kwargs = {sdk.http_client_parameter: client}
    # v1's factory was a callable; beta v2 takes the already-created httpx2
    # client.  The packaging constraint means this branch is defensive only.
    if sdk.http_client_parameter == "httpx_client_factory":
        kwargs = {sdk.http_client_parameter: lambda: client}
    return sdk.streamable_http_client(url, **kwargs)


def make_stdio_parameters(sdk: MCPSDKCompatibility, **kwargs: Any) -> Any:
    return sdk.stdio_parameters(**kwargs)


def make_stdio_context(sdk: MCPSDKCompatibility, parameters: Any) -> Any:
    return sdk.stdio_client(parameters)


def make_session(sdk: MCPSDKCompatibility, read: Any, write: Any) -> Any:
    return sdk.client_session(read, write)


def make_test_server_script() -> str:
    """Return a version-adapted local HTTP fixture used by release tests."""
    return """
import sys
from contextlib import asynccontextmanager
import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

port = int(sys.argv[1])
mcp = MCPServer("athena-http-fixture")

@mcp.tool()
def add(left: int, right: int) -> int:
    return left + right

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.headers.get("authorization") != "Bearer secret":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

app = mcp.streamable_http_app(stateless_http=True, host="127.0.0.1")
app.add_middleware(AuthMiddleware)
uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")
""".lstrip()


__all__ = [
    "MCPSDKCompatibility",
    "load_sdk",
    "make_http_client",
    "make_session",
    "make_stdio_context",
    "make_stdio_parameters",
    "make_test_server_script",
    "require_supported_sdk",
    "streamable_http_context",
]
