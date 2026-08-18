"""MCP client adapter (BUILDSPEC §90, RESEARCHSPEC "MCP trust" / "MCP namespacing").

``MCPClient`` wraps the official ``mcp`` Python SDK for a single MCP server,
supporting stdio and Streamable HTTP transports. It owns the SDK session
lifecycle (connect/close) and exposes a deliberately narrow surface: list
tools/resources/prompts, call a tool, read a resource. It is a data/tool
*source*, never an agent loop (INV-001).

The ``mcp`` SDK is an optional dependency. It is imported lazily inside the
methods that need it; if it is absent, use raises a clear :class:`MCPError`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from athena.protocol.errors import MCPError


def _require_sdk() -> Any:
    """Import the ``mcp`` SDK lazily; raise a clear error if it is absent."""
    try:
        import mcp  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - only hit when dep missing
        raise MCPError(
            "the 'mcp' package is not installed; add the 'mcp' extra, e.g. "
            "pip install 'athena[mcp]'",
        ) from exc
    return mcp


def _require_str(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise MCPError("stdio transport requires a non-empty 'command' string")
    return value


def _new_lock() -> Any:
    import asyncio

    return asyncio.Lock()


@dataclass(frozen=True)
class MCPToolRef:
    """Normalized tool descriptor returned by ``list_tools``."""

    name: str
    description: str = ""
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    annotations: Mapping[str, Any] = field(default_factory=dict)
    server: str = ""


@dataclass(frozen=True)
class MCPResourceRef:
    """Normalized resource descriptor returned by ``list_resources``."""

    uri: str
    name: str = ""
    description: str = ""
    server: str = ""


@dataclass(frozen=True)
class MCPPromptRef:
    """Normalized prompt descriptor returned by ``list_prompts``."""

    name: str
    description: str = ""
    arguments: tuple[str, ...] = ()
    server: str = ""


@dataclass(frozen=True)
class MCPToolResult:
    """Normalized outcome of a tool call or resource read."""

    is_error: bool
    content: str
    structured: Any = None


class MCPClient:
    """A single lazy, async MCP server connection (stdio or HTTP)."""

    def __init__(
        self,
        connection_id: str,
        *,
        command: str | None = None,
        args: list[str] | None = None,
        url: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        connect_timeout: float = 10.0,
    ) -> None:
        if (command is not None) == (url is not None):
            raise MCPError(
                "MCP requires exactly one transport: pass 'command' for stdio "
                "or 'url' for Streamable HTTP, not both/neither"
            )
        self.connection_id = connection_id
        self.command = command
        self.args = list(args or [])
        self.url = url
        self.env = dict(env or {})
        self.cwd = cwd
        self.connect_timeout = connect_timeout
        self._session: Any = None
        self._exit_stack: Any = None
        self._connected = False
        self._resource_cache: dict[str, object] = {}
        self._lock = _new_lock()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    @property
    def connected(self) -> bool:
        return self._connected and self._session is not None

    async def connect(self) -> "MCPClient":
        """Establish the transport and an MCP session (idempotent)."""
        if self.connected:
            return self
        mcp = _require_sdk()
        from contextlib import AsyncExitStack  # noqa: PLC0415

        stack = AsyncExitStack()
        try:
            async with self._lock:
                if self.connected:
                    return self
                if self.url is not None:
                    from mcp.client.streamable_http import (
                        streamablehttp_client,
                    )
                    http_ctx = streamablehttp_client(
                        self.url, timeout=float(self.connect_timeout)
                    )
                    read, write, _ = await stack.enter_async_context(http_ctx)
                else:
                    from mcp.client.stdio import stdio_client
                    from mcp import StdioServerParameters
                    server_params = StdioServerParameters(
                        command=_require_str(self.command),
                        args=self.args,
                        env=self.env if self.env else None,
                        cwd=self.cwd,
                    )
                    stdio_ctx = stdio_client(server_params)
                    read, write = await stack.enter_async_context(stdio_ctx)
                session = await stack.enter_async_context(
                    mcp.ClientSession(read, write)
                )
                await session.initialize()
                self._session = session
                self._exit_stack = stack
                self._connected = True
                return self
        except Exception:
            await stack.aclose()
            self._session = None
            self._exit_stack = None
            self._connected = False
            raise MCPError(
                f"failed to connect to MCP server {self.connection_id!r}: "
                "transport error or server unavailable"
            )

    async def close(self) -> None:
        """Close the connection, tolerating server/process crashes."""
        stack, self._exit_stack = self._exit_stack, None
        self._session = None
        self._connected = False
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                pass

    async def __aenter__(self) -> "MCPClient":
        return await self.connect()

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #
    def _require(self) -> Any:
        if not self.connected:
            raise MCPError(
                f"MCP server {self.connection_id!r} is not connected; "
                "call connect() first"
            )
        return self._session

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #
    async def list_tools(self) -> list[MCPToolRef]:
        """Return normalized tool descriptors for the server."""
        session = self._require()
        try:
            async with self._lock:
                result = await session.list_tools()
        except Exception as exc:
            raise MCPError(
                f"MCP list_tools failed on {self.connection_id!r}: {exc}"
            ) from exc
        out: list[MCPToolRef] = []
        for t in result.tools:
            out.append(
                MCPToolRef(
                    name=t.name,
                    description=getattr(t, "description", "") or "",
                    input_schema=(getattr(t, "inputSchema", None) or {}) or {},
                    annotations=_normalize_annotations(
                        getattr(t, "annotations", None)
                    ),
                    server=self.connection_id,
                )
            )
        return out

    async def list_resources(self) -> list[MCPResourceRef]:
        session = self._require()
        try:
            async with self._lock:
                result = await session.list_resources()
        except Exception as exc:
            raise MCPError(
                f"MCP list_resources failed on {self.connection_id!r}: {exc}"
            ) from exc
        refs = [
            MCPResourceRef(
                uri=r.uri,
                name=getattr(r, "name", "") or "",
                description=getattr(r, "description", "") or "",
                server=self.connection_id,
            )
            for r in result.resources
        ]
        self._resource_cache = {r.uri: r for r in refs}
        return refs

    async def list_prompts(self) -> list[MCPPromptRef]:
        session = self._require()
        try:
            async with self._lock:
                result = await session.list_prompts()
        except Exception as exc:
            raise MCPError(
                f"MCP list_prompts failed on {self.connection_id!r}: {exc}"
            ) from exc
        return [
            MCPPromptRef(
                name=p.name,
                description=getattr(p, "description", "") or "",
                arguments=tuple(getattr(p, "arguments", None) or []),
                server=self.connection_id,
            )
            for p in result.prompts
        ]

    # ------------------------------------------------------------------ #
    # Invocation
    # ------------------------------------------------------------------ #
    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> MCPToolResult:
        """Call a tool on the remote server; tolerate transport crashes."""
        session = self._require()
        try:
            async with self._lock:
                result = await session.call_tool(name, dict(arguments or {}))
        except Exception as exc:
            raise MCPError(
                f"mcp call_tool {name!r} failed on {self.connection_id!r}: {exc}"
            ) from exc
        return MCPToolResult(
            is_error=_is_error(result),
            content=_render_mcp_content(getattr(result, "content", None) or []),
            structured=_structured(result),
        )

    async def read_resource(self, uri: str) -> MCPToolResult:
        """Read a resource, returning normalized text content."""
        session = self._require()
        try:
            async with self._lock:
                result = await session.read_resource(uri)
        except Exception as exc:
            raise MCPError(
                f"mcp read_resource {uri!r} failed on {self.connection_id!r}: {exc}"
            ) from exc
        blocks = list(getattr(result, "contents", None) or ())
        return MCPToolResult(
            is_error=False,
            content=_render_resource_contents(blocks),
            structured=blocks,
        )


def _normalize_annotations(annotations: Any) -> dict[str, Any]:
    if annotations is None:
        return {}
    if isinstance(annotations, dict):
        return {
            "readOnlyHint": bool(annotations.get("readOnlyHint", False)),
            "destructiveHint": bool(annotations.get("destructiveHint", False)),
            "idempotentHint": bool(annotations.get("idempotentHint", False)),
            "openWorldHint": bool(annotations.get("openWorldHint", False)),
        }
    return {
        "readOnlyHint": bool(getattr(annotations, "readOnlyHint", False)),
        "destructiveHint": bool(getattr(annotations, "destructiveHint", False)),
        "idempotentHint": bool(getattr(annotations, "idempotentHint", False)),
        "openWorldHint": bool(getattr(annotations, "openWorldHint", False)),
    }


def _is_error(result: Any) -> bool:
    if isinstance(result, dict):
        return bool(result.get("isError", False))
    return bool(getattr(result, "isError", False))


def _structured(result: Any) -> Any:
    if isinstance(result, dict):
        return result
    return {
        "isError": bool(getattr(result, "isError", False)),
        "structuredContent": getattr(result, "structuredContent", None),
    }


def _render_mcp_content(blocks: list) -> str:
    parts: list[str] = []
    for b in blocks:
        if isinstance(b, dict):
            btype = b.get("type")
            if btype == "text":
                parts.append(str(b.get("text", "")))
            elif btype == "resource":
                res = b.get("resource", {})
                if isinstance(res, dict):
                    text = res.get("text")
                    if text is not None:
                        parts.append(str(text))
                    else:
                        parts.append(f"[resource:{res.get('uri','')}]")
                else:
                    parts.append(str(res))
            else:
                parts.append(str(b))
        else:
            text = getattr(b, "text", None)
            if text is not None:
                parts.append(str(text))
            else:
                parts.append(str(getattr(b, "data", "") or b))
    return "\n".join(parts)


def _render_resource_contents(blocks: list) -> str:
    parts: list[str] = []
    for b in blocks:
        if isinstance(b, dict):
            text = b.get("text")
            if text is not None:
                parts.append(str(text))
            else:
                parts.append(str(b.get("uri", "") or b))
        else:
            parts.append(getattr(b, "text", None) or str(b))
    return "\n".join(parts)


__all__ = [
    "MCPClient",
    "MCPToolRef",
    "MCPResourceRef",
    "MCPPromptRef",
    "MCPToolResult",
]