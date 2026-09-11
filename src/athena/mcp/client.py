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

import importlib
import asyncio
import inspect
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Mapping

from athena.network import validate_endpoint
from athena.network.endpoint_security import headers_are_credentialed
from athena.mcp.sdk_compat import (
    make_session,
    make_stdio_context,
    make_stdio_parameters,
    require_supported_sdk,
    streamable_http_context,
)
from athena.protocol.errors import MCPError


_MAX_DISCOVERY_ITEMS = 4096
_MAX_NAME_CHARS = 512
_MAX_DESCRIPTION_CHARS = 16_384
_MAX_CONTENT_BLOCKS = 4096
_MAX_CONTENT_CHARS = 1_048_576
_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 16_384


def _require_sdk() -> Any:
    """Import the ``mcp`` SDK lazily; raise a clear error if it is absent."""
    try:
        mcp = importlib.import_module("mcp")
    except Exception as exc:  # noqa: BLE001 - optional SDK import is normalized
        raise MCPError(
            "the 'mcp' package is not installed; add the 'mcp' extra, e.g. "
            "pip install 'athena[mcp]'",
        ) from exc
    return mcp


def _require_str(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise MCPError("stdio transport requires a non-empty 'command' string")
    return value


def _headers_are_credentialed(headers: Mapping[str, str] | None) -> bool:
    """Recognize credential-bearing headers without logging their values."""
    return headers_are_credentialed(headers)


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


@dataclass(frozen=True)
class MCPMessage:
    """One normalized message of a materialized remote prompt."""

    role: str
    text: str


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
        headers: Mapping[str, str] | None = None,
        cwd: str | None = None,
        connect_timeout: float = 10.0,
        allow_insecure_remote: bool = False,
        trust_env: bool = False,
        credentialed: bool | None = None,
        on_transport_failure: Any = None,
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
        self.headers = dict(headers or {})
        self.cwd = cwd
        self.connect_timeout = connect_timeout
        self._endpoint = (
            validate_endpoint(
                url,
                credentialed=(
                    _headers_are_credentialed(headers)
                    if credentialed is None
                    else bool(credentialed)
                ),
                allow_insecure_remote=allow_insecure_remote,
                trust_env=trust_env,
            )
            if url is not None
            else None
        )
        self._on_transport_failure = on_transport_failure
        self._session: Any = None
        self._exit_stack: Any = None
        self._connected = False
        self._last_error: str | None = None
        self._connected_at: str | None = None
        self._last_successful_connection: str | None = None
        self._tool_count = 0
        self._resource_cache: dict[str, object] = {}
        self._prompt_cache: dict[str, MCPPromptRef] = {}
        self._lock = _new_lock()
        self._background_tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    @property
    def connected(self) -> bool:
        return self._connected and self._session is not None

    def health(self) -> dict[str, Any]:
        """Return operator-safe transport and discovery health."""
        transport = "http" if self.url is not None else "stdio"
        return {
            "id": self.connection_id,
            "configured": True,
            "state": "connected"
            if self.connected
            else ("failed" if self._last_error else "stopped"),
            "transport": transport,
            "tool_count": self._tool_count,
            "last_successful_connection": self._last_successful_connection,
            "last_error": self._last_error,
        }

    async def connect(self) -> "MCPClient":
        """Establish the transport and an MCP session (idempotent)."""
        if self.connected:
            return self
        sdk = require_supported_sdk()
        from contextlib import AsyncExitStack  # noqa: PLC0415

        stack = AsyncExitStack()
        try:
            async with self._lock:
                if self.connected:
                    return self
                stale_stack, self._exit_stack = self._exit_stack, None
                self._session = None
                self._connected = False
                if stale_stack is not None:
                    try:
                        await self._bounded(stale_stack.aclose())
                    except Exception:  # noqa: BLE001 - cleanup cannot mask reconnect failure
                        # The new connection attempt owns the recovery path;
                        # retain the new error if this cleanup also fails.
                        pass
                if self.url is not None:
                    if self._endpoint is None:
                        raise MCPError("MCP HTTP endpoint policy was not initialized")
                    http_ctx = streamable_http_context(
                        sdk,
                        self.url,
                        headers=self.headers,
                        timeout=self.connect_timeout,
                        trust_env=self._endpoint.trust_env,
                    )
                    transport_streams = await self._bounded(stack.enter_async_context(http_ctx))
                    # MCP 2.x yields (read, write); retain tolerance for
                    # transitional fixtures that append a third metadata item.
                    read, write = tuple(transport_streams)[:2]
                else:
                    server_params = make_stdio_parameters(
                        sdk,
                        command=_require_str(self.command),
                        args=self.args,
                        env=self.env if self.env else None,
                        cwd=self.cwd,
                    )
                    stdio_ctx = make_stdio_context(sdk, server_params)
                    read, write = await self._bounded(stack.enter_async_context(stdio_ctx))
                session = await self._bounded(
                    stack.enter_async_context(make_session(sdk, read, write))
                )
                await self._bounded(session.initialize())
                self._session = session
                self._exit_stack = stack
                self._connected = True
                self._last_error = None
                self._connected_at = datetime.now(timezone.utc).isoformat()
                self._last_successful_connection = self._connected_at
                return self
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - normalize transport failures
            try:
                await self._bounded(stack.aclose())
            except Exception:  # noqa: BLE001 - close is best effort after transport failure
                pass
            self._session = None
            self._exit_stack = None
            self._connected = False
            self._last_error = "transport connection failed"
            raise MCPError(
                f"failed to connect to MCP server {self.connection_id!r}: "
                "transport error or server unavailable"
            ) from exc

    async def close(self) -> None:
        """Close the connection, tolerating server/process crashes."""
        current = asyncio.current_task()
        pending = [task for task in self._background_tasks if task is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._background_tasks.difference_update(pending)
        stack, self._exit_stack = self._exit_stack, None
        self._session = None
        self._connected = False
        self._connected_at = None
        if stack is not None:
            try:
                await self._bounded(stack.aclose())
            except Exception:  # noqa: BLE001 - health bookkeeping cannot mask transport failure
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
                f"MCP server {self.connection_id!r} is not connected; call connect() first"
            )
        return self._session

    async def _bounded(self, awaitable: Any) -> Any:
        """Bound SDK transport operations and process teardown.

        The official stdio transport does not apply ``connect_timeout`` to
        session initialization or exit-stack teardown. Without a host-side
        bound, a crashed or non-MCP child can hang an Athena worker forever.
        """
        return await asyncio.wait_for(awaitable, timeout=max(0.1, self.connect_timeout))

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #
    async def list_tools(self) -> list[MCPToolRef]:
        """Return normalized tool descriptors for the server."""
        session = self._require()
        try:
            async with self._lock:
                result = await self._bounded(session.list_tools())
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - normalize remote discovery failure
            self._mark_transport_failure(exc)
            raise MCPError(f"MCP list_tools failed on {self.connection_id!r}") from exc
        tools = list(getattr(result, "tools", None) or ())
        if len(tools) > _MAX_DISCOVERY_ITEMS:
            raise MCPError("MCP tool inventory exceeds the maximum allowed count")
        out: list[MCPToolRef] = []
        seen_names: set[str] = set()
        for t in tools:
            name = _bounded_text(getattr(t, "name", ""), "tool name", _MAX_NAME_CHARS)
            if name in seen_names:
                raise MCPError("MCP tool inventory contains duplicate names")
            seen_names.add(name)
            out.append(
                MCPToolRef(
                    name=name,
                    description=_bounded_text(
                        getattr(t, "description", "") or "",
                        "tool description",
                        _MAX_DESCRIPTION_CHARS,
                    ),
                    input_schema=_bounded_value(
                        (getattr(t, "inputSchema", None) or {}) or {},
                        label="tool schema",
                    ),
                    annotations=_normalize_annotations(getattr(t, "annotations", None)),
                    server=self.connection_id,
                )
            )
        self._tool_count = len(out)
        return out

    async def list_resources(self) -> list[MCPResourceRef]:
        session = self._require()
        try:
            async with self._lock:
                result = await self._bounded(session.list_resources())
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - normalize remote discovery failure
            self._mark_transport_failure(exc)
            raise MCPError(f"MCP list_resources failed on {self.connection_id!r}") from exc
        resources = list(getattr(result, "resources", None) or ())
        if len(resources) > _MAX_DISCOVERY_ITEMS:
            raise MCPError("MCP resource inventory exceeds the maximum allowed count")
        refs: list[MCPResourceRef] = []
        seen_uris: set[str] = set()
        for r in resources:
            uri = _bounded_text(getattr(r, "uri", ""), "resource URI", _MAX_NAME_CHARS)
            if uri in seen_uris:
                raise MCPError("MCP resource inventory contains duplicate URIs")
            seen_uris.add(uri)
            refs.append(
                MCPResourceRef(
                    uri=uri,
                    name=_bounded_text(
                        getattr(r, "name", "") or "", "resource name", _MAX_NAME_CHARS
                    ),
                    description=_bounded_text(
                        getattr(r, "description", "") or "",
                        "resource description",
                        _MAX_DESCRIPTION_CHARS,
                    ),
                    server=self.connection_id,
                )
            )
        self._resource_cache = {r.uri: r for r in refs}
        return refs

    async def list_prompts(self) -> list[MCPPromptRef]:
        session = self._require()
        try:
            async with self._lock:
                result = await self._bounded(session.list_prompts())
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - normalize remote discovery failure
            self._mark_transport_failure(exc)
            raise MCPError(f"MCP list_prompts failed on {self.connection_id!r}") from exc
        prompts = list(getattr(result, "prompts", None) or ())
        if len(prompts) > _MAX_DISCOVERY_ITEMS:
            raise MCPError("MCP prompt inventory exceeds the maximum allowed count")
        refs: list[MCPPromptRef] = []
        seen_names: set[str] = set()
        for p in prompts:
            name = _bounded_text(getattr(p, "name", ""), "prompt name", _MAX_NAME_CHARS)
            if name in seen_names:
                raise MCPError("MCP prompt inventory contains duplicate names")
            seen_names.add(name)
            refs.append(
                MCPPromptRef(
                    name=name,
                    description=_bounded_text(
                        getattr(p, "description", "") or "",
                        "prompt description",
                        _MAX_DESCRIPTION_CHARS,
                    ),
                    arguments=tuple(
                        _bounded_text(value, "prompt argument", _MAX_NAME_CHARS)
                        for value in list(getattr(p, "arguments", None) or [])[
                            :_MAX_DISCOVERY_ITEMS
                        ]
                    ),
                    server=self.connection_id,
                )
            )
        self._prompt_cache = {ref.name: ref for ref in refs}
        return refs

    async def get_prompt(
        self,
        name: str,
        arguments: Mapping[str, str] | None = None,
    ) -> list[MCPMessage]:
        """Materialize one remote prompt; return normalized messages.

        A prompt is remote-authored procedural text: the host treats the
        result as UNTRUSTED context (see ``mcp/prompts.py``), never as
        configured instruction.
        """
        session = self._require()
        try:
            async with self._lock:
                result = await self._bounded(session.get_prompt(name, dict(arguments or {})))
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - normalize remote prompt failure
            self._mark_transport_failure(exc)
            raise MCPError(f"mcp get_prompt {name!r} failed on {self.connection_id!r}") from exc
        messages = list(getattr(result, "messages", None) or ())
        if len(messages) > _MAX_CONTENT_BLOCKS:
            raise MCPError("MCP prompt response exceeds the maximum allowed message count")
        return [
            MCPMessage(
                role=str(getattr(message, "role", "user")),
                text=_render_mcp_content(getattr(message, "content", None) or []),
            )
            for message in getattr(result, "messages", None) or ()
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
                result = await self._bounded(session.call_tool(name, dict(arguments or {})))
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - normalize remote tool failure
            self._mark_transport_failure(exc)
            raise MCPError(f"mcp call_tool {name!r} failed on {self.connection_id!r}") from exc
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
                result = await self._bounded(session.read_resource(uri))
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - normalize remote resource failure
            self._mark_transport_failure(exc)
            raise MCPError(f"mcp read_resource {uri!r} failed on {self.connection_id!r}") from exc
        blocks = list(getattr(result, "contents", None) or ())
        return MCPToolResult(
            is_error=False,
            content=_render_resource_contents(blocks),
            structured=blocks,
        )

    def _mark_transport_failure(self, exc: BaseException) -> None:
        """Make a mid-session transport loss visible to health and policy.

        The failed session is not usable for subsequent capability calls.  We
        retain the exit stack for the explicit reconnect/close operation so a
        transport cleanup failure cannot hide the original error or strand a
        child process.  ``MCPToolExecutor`` will now fail closed instead of
        reporting a stale connected state.
        """
        self._connected = False
        self._last_error = "transport connection failed"
        self._tool_count = 0
        self._resource_cache.clear()
        self._prompt_cache.clear()
        callback = self._on_transport_failure
        if callback is not None:
            try:
                outcome = callback(self.connection_id, exc)
                if inspect.isawaitable(outcome):

                    async def _await_callback() -> None:
                        await outcome

                    task: asyncio.Task[Any] = asyncio.create_task(
                        _await_callback(), name=f"mcp-failure:{self.connection_id}"
                    )
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
            except Exception:  # noqa: BLE001 - health bookkeeping cannot mask transport failure
                # A health callback is bookkeeping; never hide the transport
                # failure that made the request unusable.
                pass


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
        value = _bounded_value(result, label="structured tool result")
        if len(str(value)) > _MAX_CONTENT_CHARS:
            raise MCPError("MCP structured tool result exceeds the maximum allowed size")
        return value
    value = {
        "isError": bool(getattr(result, "isError", False)),
        "structuredContent": getattr(result, "structuredContent", None),
    }
    if len(str(value)) > _MAX_CONTENT_CHARS:
        raise MCPError("MCP structured tool result exceeds the maximum allowed size")
    return value


def _bounded_text(value: Any, label: str, limit: int) -> str:
    text = str(value or "")
    if len(text) > limit:
        raise MCPError(f"MCP {label} exceeds the maximum allowed size")
    return text


def _bounded_value(value: Any, *, label: str, depth: int = 0, nodes: list[int] | None = None):
    """Copy JSON-like remote data while enforcing depth, nodes, and strings."""
    counter = nodes if nodes is not None else [0]
    counter[0] += 1
    if counter[0] > _MAX_JSON_NODES:
        raise MCPError(f"MCP {label} exceeds the maximum allowed JSON node count")
    if depth > _MAX_JSON_DEPTH:
        raise MCPError(f"MCP {label} exceeds the maximum allowed JSON depth")
    if isinstance(value, Mapping):
        if len(value) > _MAX_DISCOVERY_ITEMS:
            raise MCPError(f"MCP {label} exceeds the maximum allowed object size")
        return {
            _bounded_text(key, f"{label} key", _MAX_NAME_CHARS): _bounded_value(
                item,
                label=label,
                depth=depth + 1,
                nodes=counter,
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_DISCOVERY_ITEMS:
            raise MCPError(f"MCP {label} exceeds the maximum allowed array size")
        return [_bounded_value(item, label=label, depth=depth + 1, nodes=counter) for item in value]
    if isinstance(value, str):
        return _bounded_text(value, label, _MAX_CONTENT_CHARS)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _bounded_text(value, label, _MAX_CONTENT_CHARS)


def _render_mcp_content(blocks: list) -> str:
    if len(blocks) > _MAX_CONTENT_BLOCKS:
        raise MCPError("MCP content exceeds the maximum allowed block count")
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
                        parts.append(f"[resource:{res.get('uri', '')}]")
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
    rendered = "\n".join(parts)
    if len(rendered) > _MAX_CONTENT_CHARS:
        raise MCPError("MCP content exceeds the maximum allowed size")
    return rendered


def _render_resource_contents(blocks: list) -> str:
    if len(blocks) > _MAX_CONTENT_BLOCKS:
        raise MCPError("MCP resource exceeds the maximum allowed block count")
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
    rendered = "\n".join(parts)
    if len(rendered) > _MAX_CONTENT_CHARS:
        raise MCPError("MCP resource exceeds the maximum allowed size")
    return rendered


__all__ = [
    "MCPClient",
    "MCPToolRef",
    "MCPResourceRef",
    "MCPPromptRef",
    "MCPToolResult",
]
