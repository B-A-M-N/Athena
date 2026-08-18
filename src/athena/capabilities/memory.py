"""``memory`` capability (thin wrapper).

Exposes structured memory recall/save to the model. Delegates to an injected
``MemoryHandle`` (built elsewhere) so this capability is functional once the
memory subsystem lands. Effects: READ_LOCAL for recall, none for store.
"""

from __future__ import annotations

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.ids import new_id
from athena.protocol.memory import MemoryKind, MemoryRecord, MemoryScope
from athena.protocol.messages import Provenance, SourceType, TrustClass

_KIND_ALIASES = {
    "working": MemoryKind.WORKING,
    "episodic": MemoryKind.EPISODIC,
    "semantic": MemoryKind.SEMANTIC,
}
_SCOPE_ALIASES = {
    "session": MemoryScope.SESSION,
    "task": MemoryScope.TASK,
    "project": MemoryScope.PROJECT,
    "global": MemoryScope.GLOBAL,
}

_INPUT_SCHEMA = {
    "type": "object",
    "required": ["operation"],
    "properties": {
        "operation": {"type": "string", "enum": ["recall", "search", "save"]},
        "query": {"type": "string"},
        "content": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "kind": {"type": "string", "enum": list(_KIND_ALIASES)},
        "scope": {"type": "string", "enum": list(_SCOPE_ALIASES)},
        "source_id": {"type": "string"},
    },
}


class MemoryCapability:
    descriptor = CapabilityDescriptor(
        id="memory",
        description=(
            "Long-term memory: recall relevant memories by query, or persist a "
            "new memory entry. Delegates to the memory store."
        ),
        input_schema=_INPUT_SCHEMA,
        effects=frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, memory_store=None) -> None:
        self.memory_store = memory_store

    async def invoke(self, request: CapabilityRequest) -> CapabilityResult:
        args = request.arguments or {}
        op = args.get("operation", "recall")
        call_id = request.call_id or new_id("call")
        if self.memory_store is None:
            return CapabilityResult(
                call_id, request.capability_id,
                CapabilityResultStatus.FAILED,
                error="memory store not available",
            )
        if op in ("recall", "search"):
            if op == "recall":
                items = await self.memory_store.recall(
                    query=args.get("query", ""), tags=args.get("tags")
                )
            else:
                items = await self.memory_store.search(
                    query=args.get("query", ""),
                    limit=int(args.get("limit") or 10),
                )
            return CapabilityResult(
                call_id, request.capability_id, CapabilityResultStatus.OK,
                output=str(items),
            )
        if op == "save":
            kind = _KIND_ALIASES.get(
                str(args.get("kind") or "working"), MemoryKind.WORKING
            )
            scope = _SCOPE_ALIASES.get(
                str(args.get("scope") or "session"), MemoryScope.SESSION
            )
            source = Provenance(
                source_type=SourceType.MEMORY,
                source_id=args.get("source_id") or request.task_id,
                trust=TrustClass.AGENT_CURATED,
                scope=scope.value,
            )
            record = MemoryRecord(
                id=new_id("mem"),
                kind=kind,
                scope=scope,
                content=args.get("content", ""),
                source=source,
                trust=TrustClass.AGENT_CURATED,
                metadata={"tags": tuple(args.get("tags") or ())} if args.get("tags") else {},
            )
            await self.memory_store.save(record)
            return CapabilityResult(
                call_id, request.capability_id, CapabilityResultStatus.OK,
                output="saved",
                ref_uri=f"memory:{record.id}",
            )
        return CapabilityResult(
            call_id, request.capability_id, CapabilityResultStatus.FAILED,
            error=f"unknown operation: {op}",
        )


__all__ = ["MemoryCapability"]