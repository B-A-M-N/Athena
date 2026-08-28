"""``memory`` capability (thin wrapper).

Exposes structured memory recall/save to the model. Delegates to an injected
``MemoryHandle`` (built elsewhere) so this capability is functional once the
memory subsystem lands. Effects: READ_LOCAL for recall, none for store.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.ids import new_id
from athena.protocol.memory import (
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    RetrievalMode,
)
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
    "additionalProperties": False,
    "properties": {
        "operation": {"type": "string", "enum": ["recall", "search", "save"]},
        "query": {"type": "string", "maxLength": 2000},
        "content": {"type": "string", "maxLength": 10000},
        "tags": {
            "type": "array",
            "maxItems": 32,
            "items": {"type": "string", "maxLength": 128},
        },
        "kind": {"type": "string", "enum": list(_KIND_ALIASES)},
        "scope": {"type": "string", "enum": list(_SCOPE_ALIASES)},
        "scope_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "retrieval_mode": {
            "type": "string",
            "enum": [mode.value for mode in RetrievalMode],
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        "source_id": {"type": "string"},
    },
}

_MAX_OUTPUT_CHARS = 48_000
_GLOBAL_AUTHORITY = frozenset(
    {
        CapabilityRequestOrigin.USER_DIRECT,
        CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
        CapabilityRequestOrigin.SYSTEM,
    }
)


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

    async def invoke(
        self, request: CapabilityRequest, *, context=None, **kwargs
    ) -> CapabilityResult:
        # The dispatcher supplies an InvocationContext to every capability.
        # Memory uses it to bind project-scoped records and to calculate the
        # scopes visible to the requesting task. Do not discard it.
        del kwargs
        args = request.arguments or {}
        op = args.get("operation", "recall")
        call_id = request.call_id or new_id("call")
        if self.memory_store is None:
            return CapabilityResult(
                call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                error="memory store not available",
            )
        if op in ("recall", "search"):
            limit = int(args.get("limit") or 10)
            mode = RetrievalMode(str(args.get("retrieval_mode") or RetrievalMode.SEMANTIC.value))
            scopes, error = _visible_scopes(request, context, args, for_write=False)
            if error:
                return _failed(call_id, request, error)
            items: list[Any] = []
            for scope, scope_id in scopes:
                if op == "recall":
                    found = await self.memory_store.recall(
                        query=str(args.get("query") or ""),
                        tags=args.get("tags"),
                        scope=scope,
                        scope_id=scope_id,
                        mode=mode,
                        limit=limit,
                    )
                else:
                    found = await self.memory_store.search(
                        query=str(args.get("query") or ""),
                        limit=limit,
                        scope=scope,
                        scope_id=scope_id,
                        mode=mode,
                        tags=args.get("tags"),
                    )
                items.extend(found)
            items = _dedupe_and_bound(items, limit)
            return CapabilityResult(
                call_id,
                request.capability_id,
                CapabilityResultStatus.OK,
                output=_bounded_json(items, limit),
            )
        if op == "save":
            scopes, error = _visible_scopes(request, context, args, for_write=True)
            if error:
                return _failed(call_id, request, error)
            scope, scope_id = scopes[0]
            kind = _KIND_ALIASES.get(str(args.get("kind") or "working"), MemoryKind.WORKING)
            source = Provenance(
                source_type=SourceType.CAPABILITY,
                source_id=call_id,
                trust=TrustClass.AGENT_CURATED,
                scope=f"{scope.value}:{scope_id}",
            )
            record = MemoryRecord(
                id=new_id("mem"),
                kind=kind,
                scope=scope,
                content=args.get("content", ""),
                source=source,
                trust=TrustClass.AGENT_CURATED,
                metadata={"scope_id": scope_id},
                retrieval_mode=RetrievalMode(
                    str(args.get("retrieval_mode") or RetrievalMode.SEMANTIC.value)
                ),
                tags=tuple(args.get("tags") or ()),
                source_refs=((str(args["source_id"]),) if args.get("source_id") else ()),
            )
            await self.memory_store.save(record)
            return CapabilityResult(
                call_id,
                request.capability_id,
                CapabilityResultStatus.OK,
                output="saved",
                ref_uri=f"memory:{record.id}",
            )
        return CapabilityResult(
            call_id,
            request.capability_id,
            CapabilityResultStatus.FAILED,
            error=f"unknown operation: {op}",
        )


def _failed(call_id: str, request: CapabilityRequest, error: str) -> CapabilityResult:
    return CapabilityResult(
        call_id, request.capability_id, CapabilityResultStatus.FAILED, error=error
    )


def _visible_scopes(
    request: CapabilityRequest,
    context: Any,
    args: Mapping[str, Any],
    *,
    for_write: bool,
) -> tuple[list[tuple[MemoryScope, str]], str | None]:
    """Resolve the only memory scopes a request is allowed to see.

    A caller may choose a scope, but may not choose another task/session's
    identifier. Global memory is intentionally opt-in and requires a trusted
    non-model origin. The same resolver is used for writes and reads so a
    record written by the capability is found by the same rules used by
    context compilation.
    """
    requested = args.get("scope")
    if requested is not None and str(requested) not in _SCOPE_ALIASES:
        return [], f"unknown memory scope: {requested}"

    workspace = getattr(context, "workspace", None)
    owners: dict[MemoryScope, str | None] = {
        MemoryScope.SESSION: request.session_id,
        MemoryScope.TASK: request.task_id,
        MemoryScope.PROJECT: getattr(workspace, "id", None),
    }

    if requested is None:
        if for_write:
            requested_scope = MemoryScope.SESSION
        else:
            visible = [(scope, owner) for scope, owner in owners.items() if owner]
            return [(scope, str(owner)) for scope, owner in visible], None
    else:
        requested_scope = _SCOPE_ALIASES[str(requested)]

    if requested_scope is MemoryScope.GLOBAL:
        if request.origin not in _GLOBAL_AUTHORITY:
            return [], "global memory requires explicit user authority"
        scope_id = str(args.get("scope_id") or "global")
        return [(requested_scope, scope_id)], None

    owner = owners[requested_scope]
    if not owner:
        required = {
            MemoryScope.SESSION: "session_id",
            MemoryScope.TASK: "task_id",
            MemoryScope.PROJECT: "workspace",
        }[requested_scope]
        return [], f"memory scope {requested_scope.value} requires {required}"
    explicit_id = args.get("scope_id")
    if explicit_id is not None and str(explicit_id) != str(owner):
        return [], f"scope_id does not match current {requested_scope.value} scope"
    return [(requested_scope, str(owner))], None


def _dedupe_and_bound(items: list[Any], limit: int) -> list[Any]:
    by_id: dict[str, Any] = {}
    for item in items:
        key = str(item.get("id")) if isinstance(item, dict) else str(getattr(item, "id", ""))
        if key and key not in by_id:
            by_id[key] = item
    return sorted(
        by_id.values(),
        key=lambda item: (
            _record_datetime(item),
            str(item.get("id")) if isinstance(item, dict) else str(getattr(item, "id", "")),
        ),
        reverse=True,
    )[:limit]


def _record_datetime(item: Any) -> str:
    value = item.get("created_at") if isinstance(item, dict) else getattr(item, "created_at", None)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _bounded_json(items: list[Any], limit: int) -> str:
    records = [_record_to_dict(item) for item in items[:limit]]
    output = {"items": records, "count": len(records), "truncated": False}
    encoded = json.dumps(output, sort_keys=True, separators=(",", ":"), default=str)
    if len(encoded) <= _MAX_OUTPUT_CHARS:
        return encoded
    while records and len(encoded) > _MAX_OUTPUT_CHARS:
        records.pop()
        output = {"items": records, "count": len(records), "truncated": True}
        encoded = json.dumps(output, sort_keys=True, separators=(",", ":"), default=str)
    return encoded


def _record_to_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return {str(key): item[key] for key in sorted(item)}
    return {
        "id": str(getattr(item, "id", "")),
        "kind": _enum_value(getattr(item, "kind", None)),
        "scope": _enum_value(getattr(item, "scope", None)),
        "content": str(getattr(item, "content", ""))[:4000],
        "summary": getattr(item, "summary", None),
        "tags": list(getattr(item, "tags", ()) or ()),
        "scope_id": (getattr(item, "metadata", {}) or {}).get("scope_id"),
        "created_at": _record_datetime(item),
        "retrieval_mode": _enum_value(getattr(item, "retrieval_mode", None)),
        "source_id": getattr(getattr(item, "source", None), "source_id", None),
    }


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


__all__ = ["MemoryCapability"]
