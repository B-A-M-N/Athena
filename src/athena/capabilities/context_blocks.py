"""Governed capability for explicitly attached context blocks."""

from __future__ import annotations

import json
from typing import Any

from athena.context.provenance import prov
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.messages import SourceType, TrustClass


_INPUT_SCHEMA = {
    "type": "object",
    "required": ["operation"],
    "additionalProperties": False,
    "properties": {
        "operation": {
            "type": "string",
            "enum": [
                "list",
                "read",
                "create",
                "update",
                "attach",
                "detach",
                "history",
            ],
        },
        "block_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "label": {"type": "string", "minLength": 1, "maxLength": 256},
        "content": {"type": "string", "minLength": 1, "maxLength": 128_000},
        "scope": {"type": "string", "enum": ["task", "session", "project", "user", "global"]},
        "scope_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "max_tokens": {"type": "integer", "minimum": 1, "maximum": 32_000},
        "attached": {"type": "boolean"},
        "expected_version": {"type": "integer", "minimum": 1},
        "metadata": {"type": "object", "maxProperties": 32},
        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
    },
}


class ContextBlocksCapability:
    descriptor = CapabilityDescriptor(
        id="context_blocks",
        description=(
            "Manage durable, explicitly attached working context. Blocks are "
            "versioned and provenance-carrying, and are injected into relevant "
            "model turns. Operations: list/read/create/update/attach/detach/history."
        ),
        input_schema=_INPUT_SCHEMA,
        effects=frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, store) -> None:
        self._store = store

    async def invoke(self, request: CapabilityRequest, *, context=None, **kwargs):
        del kwargs
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")
        owner = _owner(request, context, args)
        if owner is None:
            return _result(request, ok=False, error="context block scope is unavailable")
        scope, scope_id = owner
        try:
            if operation == "list":
                blocks = await self._store.list(
                    scopes=_visible_scopes(request, context),
                    attached_only=bool(args.get("attached", False)),
                    limit=int(args.get("limit") or 100),
                )
                return _result(
                    request,
                    output=_json(
                        {
                            "blocks": [block.to_record() for block in blocks],
                        }
                    ),
                )

            block_id = str(args.get("block_id") or "")
            if operation == "create":
                block = await self._store.create(
                    label=str(args.get("label") or ""),
                    content=str(args.get("content") or ""),
                    scope=str(args.get("scope") or scope),
                    scope_id=str(args.get("scope_id") or scope_id),
                    trust=_trust_for_request(request),
                    max_tokens=int(args.get("max_tokens") or 2_500),
                    attached=bool(args.get("attached", True)),
                    provenance=prov(
                        SourceType.TASK,
                        source_id=request.task_id,
                        trust=_trust_for_request(request),
                        scope=str(args.get("scope") or scope),
                    ),
                    metadata=args.get("metadata") or {},
                )
                return _result(request, output=_json(block.to_record()))

            if not block_id:
                return _result(request, ok=False, error=f"{operation} requires block_id")
            if operation == "read":
                block = await self._store.get(block_id, scope=scope, scope_id=scope_id)
                return (
                    _result(request, output=_json(block.to_record()))
                    if block
                    else _missing(request)
                )
            if operation == "history":
                blocks = await self._store.history(
                    block_id,
                    scope=scope,
                    scope_id=scope_id,
                    limit=int(args.get("limit") or 100),
                )
                return _result(
                    request,
                    output=_json(
                        {
                            "block_id": block_id,
                            "versions": [block.to_record() for block in blocks],
                        }
                    ),
                )
            if operation == "update":
                block = await self._store.update(
                    block_id,
                    scope=scope,
                    scope_id=scope_id,
                    label=args.get("label"),
                    content=args.get("content"),
                    max_tokens=args.get("max_tokens"),
                    metadata=args.get("metadata"),
                    expected_version=args.get("expected_version"),
                )
                return _result(request, output=_json(block.to_record()))
            if operation in {"attach", "detach"}:
                block = await self._store.set_attached(
                    block_id,
                    scope=scope,
                    scope_id=scope_id,
                    attached=operation == "attach",
                )
                return _result(request, output=_json(block.to_record()))
            return _result(request, ok=False, error=f"unknown operation: {operation}")
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return _result(request, ok=False, error=str(exc))


def _owner(request: CapabilityRequest, context: Any, args: dict[str, Any]):
    scope = str(args.get("scope") or "")
    workspace = getattr(context, "workspace", None)
    if not scope:
        scope = "task" if request.task_id else "project"
    expected: dict[str, str] = {}
    if request.task_id:
        expected["task"] = request.task_id
    if request.session_id:
        expected["session"] = request.session_id
    if workspace is not None:
        expected["project"] = str(workspace.id)
    expected.update({"user": "athena", "global": "global"})
    if scope in expected:
        requested_id = args.get("scope_id")
        if requested_id is not None and str(requested_id) != expected[scope]:
            return None
        return scope, expected[scope]
    return None


def _visible_scopes(request: CapabilityRequest, context: Any):
    workspace = getattr(context, "workspace", None)
    scopes: list[tuple[str, str]] = []
    if request.task_id:
        scopes.append(("task", request.task_id))
    if request.session_id:
        scopes.append(("session", request.session_id))
    if workspace is not None:
        scopes.append(("project", str(workspace.id)))
    scopes.extend([("user", "athena"), ("global", "global")])
    return scopes


def _trust_for_request(request: CapabilityRequest) -> TrustClass:
    # Model-created blocks remain agent-curated; explicit user/system blocks
    # retain their stronger provenance without allowing arbitrary trust input.
    if getattr(request.origin, "value", request.origin) in {"user_direct", "system"}:
        return TrustClass.USER_CONTENT
    return TrustClass.AGENT_CURATED


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _result(request, *, ok: bool = True, output: str = "", error: str | None = None):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
    )


def _missing(request):
    return _result(request, ok=False, error="context block not found")


__all__ = ["ContextBlocksCapability"]
