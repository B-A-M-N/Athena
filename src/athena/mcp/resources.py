"""MCP resources as a context source (§90 / BHV-112).

MCP resources are NOT executable tools (§90). They integrate with the
context/artifact plane rather than being falsely represented as capabilities
(BHV-112). ``MCPResourceProvider`` reads a remote resource and produces
provider-neutral :class:`TextBlock` content carrying ``SourceType.MCP``
provenance and ``TrustClass.UNTRUSTED`` trust (§92). The
:class:`ContextCompiler` (or any consumer) pulls these blocks as lower-authority
external content; they can never override configured or authority instruction.
"""

from __future__ import annotations

from athena.protocol.messages import (
    ContentBlock,
    Provenance,
    SourceType,
    TextBlock,
    TrustClass,
    utcnow,
)

from athena.mcp.client import MCPClient, MCPResourceRef


def mcp_provenance(
    connection_id: str,
    *,
    source_id: str | None = None,
    tier: str = "data",
) -> Provenance:
    """Build UNTRUSTED MCP provenance for injected resource content."""
    return Provenance(
        source_type=SourceType.MCP,
        source_id=source_id or f"mcp:{connection_id}",
        trust=TrustClass.UNTRUSTED,
        scope=f"mcp:{tier}",
        created_at=utcnow(),
    )


class MCPResourceProvider:
    """Context provider exposing remote MCP resources as content blocks.

    This is a context *source*, not a capability (BHV-112). Each block carries
    MCP SourceType + UNTRUSTED provenance so downstream consumers treat it as
    external, lower-authority content.
    """

    def __init__(self, clients: dict[str, MCPClient] | None = None) -> None:
        self._clients: dict[str, MCPClient] = dict(clients or {})

    def add_client(self, connection_id: str, client: MCPClient) -> None:
        """Register a client whose resource cache the provider can use."""
        self._clients[connection_id] = client

    def available(self) -> list[MCPResourceRef]:
        """Return the union of discovered resource refs across clients."""
        out: list[MCPResourceRef] = []
        seen: set[str] = set()
        for client in self._clients.values():
            for ref in self._cached(client):
                if ref.uri not in seen:
                    seen.add(ref.uri)
                    out.append(ref)
        return out

    async def read_resource_blocks(
        self,
        uri: str,
        *,
        connection_id: str | None = None,
    ) -> list[ContentBlock]:
        """Read a resource and return UNTRUSTED MCP content blocks."""
        client = self._pick(connection_id, uri)
        result = await client.read_resource(uri)
        provenance = mcp_provenance(client.connection_id, source_id=uri)
        return [
            TextBlock(type="text", text=result.content, provenance=provenance),
        ]

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #
    def _pick(self, connection_id: str | None, uri: str) -> MCPClient:
        if connection_id is not None:
            client = self._clients.get(connection_id)
            if client is None:
                raise LookupError(f"unknown MCP connection: {connection_id}")
            return client
        if len(self._clients) == 1:
            return next(iter(self._clients.values()))
        for client in self._clients.values():
            if uri in client._resource_cache:
                return client
        raise LookupError(
            "cannot resolve MCP resource; specify connection_id or list resources first"
        )

    @staticmethod
    def _cached(client: MCPClient) -> list[MCPResourceRef]:
        cache = getattr(client, "_resource_cache", None) or {}
        return [r for r in cache.values() if isinstance(r, MCPResourceRef)]


__all__ = ["MCPResourceProvider", "mcp_provenance"]
