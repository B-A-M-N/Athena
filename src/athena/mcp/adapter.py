"""MCP adapter (§90, §91, §92 / INV-001, INV-004).

``MCPAdapter`` is the bridge from MCP servers into Athena's single capability
path: it translates remote MCP tools into :class:`CapabilityDescriptor`
objects, registers them (under collision-free canonical ids) with a
:class:`CapabilityRegistry`, and provides the :class:`CapabilityExecutor` that
routes each invocation back through an :class:`MCPClient` to the remote tool.

It spawns no agent loop (INV-001): a call becomes a normal capability
invocation that the kernel dispatches via CapabilityRegistry -> PolicyEngine ->
executor (INV-004 / BHV-108). Trust is handled per §92 / BHV-111: every
MCP-served capability is marked UNTRUSTED and server annotations are recorded
as metadata only, never as authorization; the policy engine remains
authoritative.
"""

from __future__ import annotations

from typing import Any

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
)
from athena.protocol.errors import MCPError

from athena.mcp.client import MCPClient, MCPToolRef
from athena.mcp.tools import (
    canonical_capability_id,
    effect_note,
    friendly_alias,
    infer_effects,
    tool_schema_to_descriptor_input,
)


class MCPToolExecutor:
    """A registered capability executor that calls a remote MCP tool.

    ``descriptor`` carries the UNTRUSTED marker and inferred effects so policy
    gates the call (INV-004). ``invoke`` translates the verified, dispatched
    request into an MCP tool call and normalizes the result.
    """

    def __init__(
        self,
        descriptor: CapabilityDescriptor,
        client: MCPClient,
        tool_name: str,
    ) -> None:
        self.descriptor = descriptor
        self._client = client
        self._tool_name = tool_name

    async def invoke(
        self,
        request: CapabilityRequest,
        *,
        output_accumulator: Any = None,
        context: Any = None,
    ) -> CapabilityResult:
        """Invoke the remote tool through the dispatcher executor protocol.

        The dispatcher supplies the same optional streaming and invocation
        context arguments to every capability executor. MCP tools do not use
        either value, but accepting them keeps the MCP executor on the
        canonical capability path.
        """
        if not self._client.connected:
            return _fail(request, "MCP server is not connected")
        result = await self._client.call_tool(self._tool_name, dict(request.arguments or {}))
        metadata = {"mcp": True}
        if result.structured is not None:
            metadata["structured"] = result.structured
        call_id = getattr(request, "call_id", "")
        return CapabilityResult(
            call_id,
            request.capability_id,
            CapabilityResultStatus.FAILED if result.is_error else CapabilityResultStatus.OK,
            output=result.content,
            error=result.content if result.is_error else None,
            metadata=metadata,
        )


class MCPAdapter:
    """Discovers MCP tools and exposes them as registry capabilities."""

    def __init__(
        self,
        registry: Any,
        *,
        origin: CapabilityOrigin = CapabilityOrigin.MCP,
    ) -> None:
        self.registry = registry
        self.origin = origin
        self._executors: dict[str, MCPToolExecutor] = {}
        self._aliases: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # Translation
    # ------------------------------------------------------------------ #
    def build_descriptor(
        self,
        tool: MCPToolRef,
        *,
        connection_id: str,
        server_alias: str = "",
    ) -> CapabilityDescriptor:
        """Build a CapabilityDescriptor for one MCP tool (UNTRUSTED)."""
        capability_id = canonical_capability_id(connection_id, tool.name)
        if capability_id in self._executors:
            raise MCPError(f"capability already registered: {capability_id}")
        effects = infer_effects(tool.name, tool.annotations, tool.input_schema, remote=True)
        alias = friendly_alias(server_alias or connection_id, tool.name)
        note = effect_note(tool.annotations)
        description = f"[MCP {alias}] {tool.description or tool.name}" + (
            f" ({note})" if note else ""
        )
        tags = {"mcp", "external", "untrusted"}
        if server_alias:
            tags.add(f"server:{server_alias}")
        return CapabilityDescriptor(
            id=capability_id,
            description=description,
            input_schema=tool_schema_to_descriptor_input(tool.input_schema),
            effects=effects,
            tags=frozenset(tags),
            origin=self.origin,
        )

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #
    def register_tool(
        self,
        tool: MCPToolRef,
        *,
        connection_id: str,
        client: MCPClient,
        server_alias: str = "",
    ) -> CapabilityDescriptor:
        descriptor = self.build_descriptor(
            tool, connection_id=connection_id, server_alias=server_alias
        )
        executor = MCPToolExecutor(descriptor, client, tool.name)
        self.registry.register(executor)
        self._executors[descriptor.id] = executor
        self._aliases[friendly_alias(server_alias or connection_id, tool.name)] = descriptor.id
        return descriptor

    def register_all(
        self,
        tools: list[MCPToolRef],
        *,
        connection_id: str,
        client: MCPClient,
        server_alias: str = "",
    ) -> list[CapabilityDescriptor]:
        return [
            self.register_tool(
                tool,
                connection_id=connection_id,
                client=client,
                server_alias=server_alias,
            )
            for tool in tools
        ]

    async def collect_and_register(
        self,
        client: MCPClient,
        *,
        server_alias: str = "",
    ) -> list[CapabilityDescriptor]:
        """Discover all tools from a connected client and register them."""
        tools = await client.list_tools()
        return self.register_all(
            tools,
            connection_id=client.connection_id,
            client=client,
            server_alias=server_alias,
        )

    # ------------------------------------------------------------------ #
    # Lookup
    # ------------------------------------------------------------------ #
    def resolve_alias(self, alias: str) -> str | None:
        """Map a friendly ``server.tool`` alias back to a canonical id."""
        return self._aliases.get(alias)

    def capability_ids(self) -> list[str]:
        return sorted(self._executors)

    def unregister_connection(self, connection_id: str) -> list[str]:
        """Remove every tool registered from one MCP connection."""
        wanted = str(connection_id)
        removed: list[str] = []
        for capability_id, executor in list(self._executors.items()):
            if executor._client.connection_id != wanted:  # noqa: SLF001
                continue
            self.registry.unregister(capability_id)
            self._executors.pop(capability_id, None)
            for alias, target in list(self._aliases.items()):
                if target == capability_id:
                    self._aliases.pop(alias, None)
            removed.append(capability_id)
        return removed


def _fail(request: CapabilityRequest, message: str) -> CapabilityResult:
    return CapabilityResult(
        getattr(request, "call_id", ""),
        request.capability_id,
        CapabilityResultStatus.FAILED,
        error=message,
    )


__all__ = ["MCPAdapter", "MCPToolExecutor"]
