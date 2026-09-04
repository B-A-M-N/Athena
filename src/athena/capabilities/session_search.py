"""``session_search`` capability — full-text search of conversation history.

Exposes the durable message transcript's FTS index to the model. Scope is
explicit and closed: the current session by default, additional session ids
only through explicit caller authority. This keeps prior conversation history
distinct from semantic memory: transcript recall is provenance-anchored
(session, message, timestamp), never a memory write.
"""

from __future__ import annotations

import json
from typing import Any

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

# Origins allowed to widen the search beyond the requesting session.
_WIDEN_AUTHORITY = frozenset(
    {
        CapabilityRequestOrigin.USER_DIRECT,
        CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
        CapabilityRequestOrigin.SYSTEM,
    }
)

_INPUT_SCHEMA = {
    "type": "object",
    "required": ["query"],
    "additionalProperties": False,
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 2000},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        "context_window": {"type": "integer", "minimum": 0, "maximum": 20},
        "session_ids": {
            "type": "array",
            "maxItems": 16,
            "items": {"type": "string", "minLength": 1, "maxLength": 128},
        },
    },
}

_MAX_OUTPUT_CHARS = 48_000


class SessionSearchCapability:
    descriptor = CapabilityDescriptor(
        id="session_search",
        description=(
            "Full-text search over past conversation history. Returns matching "
            "messages with session, timestamp, and task provenance, optionally "
            "with surrounding context. Scoped to the current session unless "
            "explicitly widened."
        ),
        tags=frozenset({"history", "transcript", "search", "conversation", "earlier"}),
        input_schema=_INPUT_SCHEMA,
        effects=frozenset({EffectClass.READ_LOCAL}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, message_store=None) -> None:
        self._messages = message_store

    async def invoke(
        self, request: CapabilityRequest, *, context=None, **kwargs
    ) -> CapabilityResult:
        del kwargs
        call_id = request.call_id or new_id("call")
        if self._messages is None:
            return CapabilityResult(
                call_id,
                self.descriptor.id,
                CapabilityResultStatus.FAILED,
                error="message store not available",
            )
        args = dict(request.arguments or {})
        query = str(args.get("query") or "").strip()
        if not query:
            return CapabilityResult(
                call_id,
                self.descriptor.id,
                CapabilityResultStatus.FAILED,
                error="query is required",
            )
        # Closed scope: the requesting session by default. Widening to other
        # sessions requires caller authority — a model-issued call from inside
        # a task cannot enumerate sessions it was never told about.
        session_ids: list[str] = [request.session_id] if request.session_id else []
        requested = [str(s) for s in (args.get("session_ids") or ()) if str(s).strip()]
        if requested:
            origin = getattr(request, "origin", None)
            if origin in _WIDEN_AUTHORITY:
                for extra in requested:
                    if extra not in session_ids:
                        session_ids.append(extra)
        if not session_ids:
            return CapabilityResult(
                call_id,
                self.descriptor.id,
                CapabilityResultStatus.FAILED,
                error="no session scope available for this request",
            )
        limit = int(args.get("limit") or 20)
        context_window = int(args.get("context_window") or 0)
        try:
            hits = await self._messages.search(
                query,
                session_ids=tuple(session_ids),
                limit=limit,
                context_window=context_window,
            )
        except Exception as exc:
            return CapabilityResult(
                call_id,
                self.descriptor.id,
                CapabilityResultStatus.FAILED,
                error=f"session search failed: {exc}",
            )
        payload: dict[str, Any] = {
            "query": query,
            "scope": session_ids,
            "matches": hits,
            "count": len(hits),
        }
        output = json.dumps(payload, default=str)
        if len(output) > _MAX_OUTPUT_CHARS:
            payload["matches"] = payload["matches"][: max(1, len(payload["matches"]) // 2)]
            payload["truncated"] = True
            output = json.dumps(payload, default=str)
        return CapabilityResult(
            call_id,
            self.descriptor.id,
            CapabilityResultStatus.OK,
            output=output,
            metadata={"operation": "search", "count": len(hits)},
        )


__all__ = ["SessionSearchCapability"]
