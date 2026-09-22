"""``session_search`` capability — full-text search of conversation history.

Exposes the durable message transcript's FTS index to the model. Scope is
explicit and closed: the current session by default, additional session ids
only through explicit caller authority. This keeps prior conversation history
distinct from semantic memory: transcript recall is provenance-anchored
(session, message, timestamp), never a memory write.
"""

from __future__ import annotations
from athena.capabilities.operations import native_descriptor

import json
from typing import Any

from athena.protocol.capabilities import (
    CapabilityFailure,
    CapabilityFailureCode,
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
    "required": [],
    "additionalProperties": False,
    "properties": {
        "operation": {"type": "string", "enum": ["search", "read"]},
        "query": {"type": "string", "minLength": 1, "maxLength": 2000},
        "scope": {"type": "string", "enum": ["current_session", "principal"]},
        "project_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        "context_window": {"type": "integer", "minimum": 0, "maximum": 20},
        "session_ids": {
            "type": "array",
            "maxItems": 16,
            "items": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "session_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "anchor": {"type": "string", "minLength": 1, "maxLength": 128},
        "before": {"type": "integer", "minimum": 0, "maximum": 20},
        "after": {"type": "integer", "minimum": 0, "maximum": 20},
    },
}

_MAX_OUTPUT_CHARS = 48_000


def _typed_failed(
    request: CapabilityRequest, msg: str, code: CapabilityFailureCode
) -> CapabilityResult:
    return CapabilityResult.failure(
        request, CapabilityFailure(code=code, detail=msg, stage="invoke")
    )


class SessionSearchCapability:
    descriptor = native_descriptor(
        id="session_search",
        description=(
            "Full-text search over past conversation history. Returns matching "
            "messages with session, timestamp, and task provenance, optionally "
            "with surrounding context. Scoped to the current session unless "
            "explicitly widened. A principal scope is resolved by the host and "
            "does not accept model-supplied session ids. Read can recover a "
            "bounded window around a returned message anchor."
        ),
        tags=frozenset({"history", "transcript", "search", "conversation", "earlier"}),
        input_schema=_INPUT_SCHEMA,
        effects=frozenset({EffectClass.READ_LOCAL}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, message_store=None) -> None:
        self._messages = message_store

    def _conformance_failure_probe(self) -> dict:
        failure = CapabilityFailure(
            code=CapabilityFailureCode.INVALID_INPUT,
            detail="conformance probe",
            stage="invoke",
        )
        return failure.to_metadata()

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
        operation = str(args.get("operation") or "search")
        principal_id = getattr(context, "principal_id", None)
        workspace_id = getattr(getattr(context, "workspace", None), "id", None)
        requested_project = str(args.get("project_id") or "") or None
        if requested_project and workspace_id and requested_project != str(workspace_id):
            return CapabilityResult(
                call_id,
                self.descriptor.id,
                CapabilityResultStatus.FAILED,
                error="project history is limited to the current workspace",
            )
        project_id = requested_project
        if operation == "read":
            anchor = str(args.get("anchor") or "").strip()
            if not anchor:
                return _typed_failed(
                    request,
                    "anchor is required for read",
                    CapabilityFailureCode.INVALID_INPUT,
                )
            requested_session = str(args.get("session_id") or request.session_id or "") or None
            if context is not None and not principal_id:
                return CapabilityResult(
                    call_id,
                    self.descriptor.id,
                    CapabilityResultStatus.FAILED,
                    error="principal identity is required for historical reads",
                )
            try:
                detail = await self._messages.read_context(
                    anchor,
                    session_id=requested_session,
                    principal_id=principal_id,
                    project_id=project_id,
                    before=int(args.get("before") or 3),
                    after=int(args.get("after") or 3),
                )
            except Exception as exc:
                return CapabilityResult(
                    call_id,
                    self.descriptor.id,
                    CapabilityResultStatus.FAILED,
                    error=f"session read failed: {exc}",
                )
            if detail is None:
                return CapabilityResult(
                    call_id,
                    self.descriptor.id,
                    CapabilityResultStatus.FAILED,
                    error="message anchor not found in the permitted history",
                )
            return CapabilityResult(
                call_id,
                self.descriptor.id,
                CapabilityResultStatus.OK,
                output=json.dumps(detail, default=str),
                metadata={"operation": "read", "anchor": anchor},
            )

        query = str(args.get("query") or "").strip()
        if not query:
            return _typed_failed(request, "query is required", CapabilityFailureCode.INVALID_INPUT)
        scope = str(args.get("scope") or "current_session")
        if scope not in {"current_session", "principal"}:
            return CapabilityResult(
                call_id,
                self.descriptor.id,
                CapabilityResultStatus.FAILED,
                error="scope must be current_session or principal",
            )
        # Closed scope: the requesting session by default. Principal scope is
        # resolved entirely by the host so model text cannot widen ownership.
        if scope == "principal":
            if not principal_id:
                return CapabilityResult(
                    call_id,
                    self.descriptor.id,
                    CapabilityResultStatus.FAILED,
                    error="principal identity is required for principal history",
                )
            session_ids: list[str] = []
        else:
            session_ids = [request.session_id] if request.session_id else []
        requested = [str(s) for s in (args.get("session_ids") or ()) if str(s).strip()]
        if requested and scope == "current_session":
            origin = getattr(request, "origin", None)
            if origin in _WIDEN_AUTHORITY:
                for extra in requested:
                    if extra not in session_ids:
                        session_ids.append(extra)
        if not session_ids and not principal_id:
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
                principal_id=principal_id,
                project_id=project_id if scope == "principal" else None,
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
            "scope": session_ids if scope == "current_session" else "principal",
            "scope_kind": scope,
            "sessions": session_ids,
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
            metadata={"operation": "search", "scope": scope, "count": len(hits)},
        )


__all__ = ["SessionSearchCapability"]
