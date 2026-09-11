"""MCP prompts as untrusted procedural context (P1-18).

An MCP prompt is remote-authored procedural text: a reusable workflow
template a server exposes by name. Parity with tools/resources means
the agent can DISCOVER what prompts exist and MATERIALIZE one into
context — as UNTRUSTED external content (SourceType.MCP,
TrustClass.UNTRUSTED, §92), never as configured instruction, never
auto-selected into every turn. Selection is explicit: a task (operator
or model, through a named reference) asks for a prompt by server+name
with its arguments.

This mirrors :mod:`athena.mcp.resources`: a context *source*, not a
capability (BHV-112) — materializing a prompt does not execute anything;
it only contributes lower-authority text the compiler may drop under
budget pressure like any other droppable block.
"""

from __future__ import annotations

from fnmatch import fnmatchcase

from athena.protocol.messages import (
    ContentBlock,
    Provenance,
    SourceType,
    TextBlock,
    TrustClass,
    utcnow,
)

from athena.mcp.client import MCPClient, MCPPromptRef


def mcp_prompt_provenance(
    connection_id: str,
    prompt_name: str,
) -> Provenance:
    """Build UNTRUSTED provenance for materialized prompt content."""
    return Provenance(
        source_type=SourceType.MCP,
        source_id=f"mcp:{connection_id}:prompt:{prompt_name}",
        trust=TrustClass.UNTRUSTED,
        scope="mcp:prompt",
        created_at=utcnow(),
    )


class MCPPromptProvider:
    """Context provider discovering and materializing remote MCP prompts."""

    def __init__(self, clients: dict[str, MCPClient] | None = None) -> None:
        self._clients: dict[str, MCPClient] = dict(clients or {})
        self._policies: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}

    def add_client(
        self,
        connection_id: str,
        client: MCPClient,
        *,
        allowed: tuple[str, ...] = (),
        denied: tuple[str, ...] = (),
    ) -> None:
        self._clients[connection_id] = client
        self._policies[connection_id] = (tuple(allowed), tuple(denied))

    def remove_client(self, connection_id: str) -> None:
        """Remove a dead connection so prompts are not advertised stale."""
        self._clients.pop(str(connection_id), None)
        self._policies.pop(str(connection_id), None)

    async def available(self) -> list[MCPPromptRef]:
        """Discover prompts across all connected servers (live query)."""
        out: list[MCPPromptRef] = []
        seen: set[tuple[str, str]] = set()
        for connection_id, client in self._clients.items():
            try:
                refs = await client.list_prompts()
            except Exception:
                continue  # discovery is best-effort; one dead server stays dead
            for ref in refs:
                if not self._visible(connection_id, ref.name):
                    continue
                key = (ref.server or connection_id, ref.name)
                if key in seen:
                    continue
                seen.add(key)
                out.append(ref)
        return out

    async def render_prompt_blocks(
        self,
        name: str,
        arguments: dict[str, str] | None = None,
        *,
        connection_id: str | None = None,
    ) -> list[ContentBlock]:
        """Materialize one prompt into UNTRUSTED context blocks.

        The returned blocks are droppable context: the compiler treats
        them like any other external content — boundable, droppable under
        budget pressure, and never authority.
        """
        client = self._pick(connection_id, name)
        if not self._visible(client.connection_id, name):
            raise LookupError(f"MCP prompt {name!r} is not exposed by policy")
        messages = await client.get_prompt(name, arguments or {})
        provenance = mcp_prompt_provenance(client.connection_id, name)
        blocks: list[ContentBlock] = []
        for message in messages:
            text = (message.text or "").strip()
            if not text:
                continue
            blocks.append(
                TextBlock(
                    type="text",
                    # Role-preserving wrapper keeps assistant-authored
                    # prompts from impersonating a user instruction to the
                    # host: the wrapper text states the origin either way.
                    text=(
                        f"[MCP prompt {name!r} from server {client.connection_id!r}; "
                        f"remote-authored procedural context, not an instruction]\n"
                        f"{message.role}: {text}"
                    ),
                    provenance=provenance,
                )
            )
        return blocks

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #
    def _pick(self, connection_id: str | None, name: str) -> MCPClient:
        if connection_id is not None:
            client = self._clients.get(connection_id)
            if client is None:
                raise LookupError(f"unknown MCP connection: {connection_id}")
            if not self._visible(connection_id, name):
                raise LookupError(f"MCP prompt {name!r} is not exposed by policy")
            return client
        if len(self._clients) == 1:
            return next(iter(self._clients.values()))
        for client in self._clients.values():
            cache = getattr(client, "_prompt_cache", None) or {}
            if self._visible(client.connection_id, name) and (
                name in cache or any(ref.name == name for ref in cache.values())
            ):
                return client
        raise LookupError("cannot resolve MCP prompt; specify connection_id or list prompts first")

    def _visible(self, connection_id: str, name: str) -> bool:
        allowed, denied = self._policies.get(connection_id, ((), ()))
        if any(fnmatchcase(str(name), pattern) for pattern in denied):
            return False
        return not allowed or any(fnmatchcase(str(name), pattern) for pattern in allowed)


__all__ = ["MCPPromptProvider", "mcp_prompt_provenance"]
