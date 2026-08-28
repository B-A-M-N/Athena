"""Model-facing persistent external specialist delegation."""

from __future__ import annotations

import json

from athena.delegates.sessions import ExternalDelegateManager
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)


class ExternalDelegateCapability:
    descriptor = CapabilityDescriptor(
        id="delegate.external",
        description=(
            "Consult a host-registered external specialist through a persistent "
            "governed session. Operations: list, start, send, status, close. "
            "Remote capability requests return through Athena policy and budgets."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["list", "start", "send", "status", "close"],
                },
                "specialist": {"type": "string", "maxLength": 128},
                "session_id": {"type": "string", "maxLength": 128},
                "objective": {"type": "string", "maxLength": 20_000},
                "context": {"type": "array", "maxItems": 64, "items": {"type": "object"}},
            },
            "additionalProperties": False,
        },
        effects=frozenset(
            {EffectClass.READ_LOCAL, EffectClass.SPAWN_PROCESS, EffectClass.EXTERNAL_MESSAGE}
        ),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, manager: ExternalDelegateManager) -> None:
        self._manager = manager

    async def invoke(self, request: CapabilityRequest, *, context=None, **kwargs):
        del kwargs
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")
        try:
            if operation == "list":
                return _result(
                    request,
                    output=json.dumps(
                        await self._manager.list(task_id=request.task_id), default=str
                    ),
                )
            if request.task_id is None:
                return _result(request, ok=False, error="external delegation requires a task")
            workspace = getattr(context, "workspace", None)
            if workspace is None:
                return _result(
                    request, ok=False, error="external delegation requires workspace context"
                )
            if operation == "start":
                specialist = str(args.get("specialist") or "")
                objective = str(args.get("objective") or "")
                if not specialist or not objective:
                    return _result(
                        request, ok=False, error="start requires specialist and objective"
                    )
                session = await self._manager.start(
                    specialist,
                    task_id=request.task_id,
                    session_id=request.session_id,
                    workspace=workspace,
                    context=tuple(args.get("context") or ()),
                )
                # Send the objective after the persistent session handshake.
                response = await self._manager.send(
                    session.id,
                    task_id=request.task_id,
                    objective=objective,
                    workspace=workspace,
                    context=tuple(args.get("context") or ()),
                    task_policy=getattr(context, "capability_policy", None),
                    task_budget=getattr(context, "resource_budget", None),
                )
                return _result(
                    request,
                    output=json.dumps(
                        {
                            "session": session.to_record(),
                            "response": response,
                        },
                        default=str,
                    ),
                    ref_uri=f"delegate-session:{session.id}",
                )
            session_id = str(args.get("session_id") or "")
            if not session_id:
                return _result(request, ok=False, error=f"{operation} requires session_id")
            if operation == "send":
                objective = str(args.get("objective") or "")
                if not objective:
                    return _result(request, ok=False, error="send requires objective")
                response = await self._manager.send(
                    session_id,
                    task_id=request.task_id,
                    objective=objective,
                    workspace=workspace,
                    context=tuple(args.get("context") or ()),
                    task_policy=getattr(context, "capability_policy", None),
                    task_budget=getattr(context, "resource_budget", None),
                )
                return _result(
                    request,
                    output=json.dumps(response, default=str),
                    ref_uri=f"delegate-session:{session_id}",
                )
            if operation == "status":
                value = await self._manager.status(session_id, task_id=request.task_id)
            elif operation == "close":
                value = {
                    "session_id": session_id,
                    "closed": await self._manager.close(session_id, task_id=request.task_id),
                }
            else:
                return _result(request, ok=False, error=f"unknown operation: {operation}")
            return _result(
                request,
                output=json.dumps(value, default=str),
                ref_uri=f"delegate-session:{session_id}",
            )
        except (KeyError, OSError, PermissionError, RuntimeError, TypeError, ValueError) as exc:
            return _result(request, ok=False, error=str(exc))


def _result(request, *, ok=True, output="", error=None, ref_uri=None):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
        ref_uri=ref_uri,
    )


__all__ = ["ExternalDelegateCapability"]
