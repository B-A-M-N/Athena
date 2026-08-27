"""``delegate`` capability.

Spawns a child Task via an injected TaskManager/Delegation handle (BUILDSPEC
section 4.7 / 69). Effect is tagged so policy can gate delegation.

The model-facing primitive supports structured operations (P0-17):

    delegate.spawn   -> create + enqueue a child, return child id
    delegate.status  -> query child status (RUNNING/COMPLETE/etc.)
    delegate.collect -> wait for and return the child's TaskResult
    delegate.cancel  -> cancel the child
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
from athena.protocol.tasks import ContextRef, TaskStatus

_OPERATIONS = ("spawn", "status", "collect", "cancel")

_INPUT_SCHEMA = {
    "type": "object",
    "required": ["operation"],
    "properties": {
        "operation": {"type": "string", "enum": list(_OPERATIONS)},
        "objective": {"type": "string"},
        "child_task_id": {"type": "string"},
        "parent_task_id": {"type": "string"},
        "timeout": {"type": "number"},
        "metadata": {"type": "object"},
    },
}


class DelegateCapability:
    descriptor = CapabilityDescriptor(
        id="delegate",
        description=(
            "Delegate a unit of work to a child Task. Supports spawn (create and "
            "enqueue a child, returning its id), status (query the child state), "
            "collect (wait for and return the child result), and cancel."
        ),
        input_schema=_INPUT_SCHEMA,
        effects=frozenset({EffectClass.READ_LOCAL, EffectClass.SPAWN_PROCESS}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, delegation_handle=None) -> None:
        self._handle = delegation_handle

    async def invoke(
        self,
        request: CapabilityRequest,
        *,
        output_accumulator=None,
        context=None,
    ) -> CapabilityResult:
        args = dict(request.arguments or {})
        call_id = request.call_id or new_id("call")
        if self._handle is None:
            return CapabilityResult(
                call_id, request.capability_id,
                CapabilityResultStatus.FAILED,
                error="delegation handle not available",
            )
        operation = str(args.get("operation") or "spawn")
        try:
            if operation == "spawn":
                return await self._spawn(request, call_id, args)
            if operation == "status":
                return await self._status(args, call_id)
            if operation == "collect":
                return await self._collect(args, call_id)
            if operation == "cancel":
                return await self._cancel(args, call_id)
            return CapabilityResult(
                call_id, request.capability_id,
                CapabilityResultStatus.FAILED,
                error=f"unknown delegate operation: {operation}",
            )
        except Exception as exc:
            return CapabilityResult(
                call_id, request.capability_id,
                CapabilityResultStatus.FAILED,
                error=f"delegate.{operation} failed: {exc}",
            )

    async def _spawn(
        self, request: CapabilityRequest, call_id: str, args: dict
    ) -> CapabilityResult:
        objective = args.get("objective") or ""
        context = self._decode_context(args.get("context") or ())
        child_id = await self._handle.spawn_child(
            objective=objective,
            parent_task_id=args.get("parent_task_id") or request.task_id,
            metadata=args.get("metadata") or {},
            context=context,
        )
        return CapabilityResult(
            call_id, request.capability_id, CapabilityResultStatus.OK,
            output=f"delegated to {child_id}",
            ref_uri=f"task:{child_id}",
            metadata={"operation": "spawn", "child_task_id": child_id,
                      "status": TaskStatus.QUEUED.value},
        )

    async def _status(self, args: dict, call_id: str) -> CapabilityResult:
        child_id = args.get("child_task_id")
        if not child_id:
            return CapabilityResult(
                call_id, self.descriptor.id, CapabilityResultStatus.FAILED,
                error="delegate.status requires child_task_id",
            )
        status = await self._handle.status_of(child_id)
        return CapabilityResult(
            call_id, self.descriptor.id, CapabilityResultStatus.OK,
            output=f"child {child_id} status: {status.value}",
            ref_uri=f"task:{child_id}",
            metadata={"operation": "status", "child_task_id": child_id,
                     "status": status.value},
        )

    async def _collect(self, args: dict, call_id: str) -> CapabilityResult:
        child_id = args.get("child_task_id")
        if not child_id:
            return CapabilityResult(
                call_id, self.descriptor.id, CapabilityResultStatus.FAILED,
                error="delegate.collect requires child_task_id",
            )
        timeout = args.get("timeout")
        result = await self._handle.collect(
            child_id, timeout=timeout if timeout is None else float(timeout),
        )
        return CapabilityResult(
            call_id, self.descriptor.id, _result_status(result),
            output=_format_result(result),
            ref_uri=f"task:{child_id}",
            metadata={
                "operation": "collect",
                "child_task_id": child_id,
                "status": result.status.value,
                "summary": result.summary,
            },
        )

    async def _cancel(self, args: dict, call_id: str) -> CapabilityResult:
        child_id = args.get("child_task_id")
        if not child_id:
            return CapabilityResult(
                call_id, self.descriptor.id, CapabilityResultStatus.FAILED,
                error="delegate.cancel requires child_task_id",
            )
        status = await self._handle.cancel_child(child_id)
        return CapabilityResult(
            call_id, self.descriptor.id, CapabilityResultStatus.OK,
            output=f"cancelled child {child_id}",
            ref_uri=f"task:{child_id}",
            metadata={"operation": "cancel", "child_task_id": child_id,
                     "status": status.value},
        )

    @staticmethod
    def _decode_context(raw) -> tuple[ContextRef, ...]:
        refs = []
        for item in raw or ():
            if isinstance(item, ContextRef):
                refs.append(item)
            elif isinstance(item, dict):
                refs.append(ContextRef(
                    kind=item.get("kind", "task"),
                    ref=item.get("ref", ""),
                    source_id=item.get("source_id"),
                    summary=item.get("summary"),
                ))
        return tuple(refs)


def _format_result(result) -> str:
    return (
        f"child {result.task_id} {result.status.value}: {result.summary}"
        if result.summary else f"child {result.task_id} {result.status.value}"
    )


def _result_status(result) -> CapabilityResultStatus:
    if result.status == TaskStatus.CANCELLED:
        return CapabilityResultStatus.CANCELLED
    if result.status in (TaskStatus.COMPLETE, TaskStatus.PARTIAL, TaskStatus.FAILED):
        return CapabilityResultStatus.OK
    return CapabilityResultStatus.OK


__all__ = ["DelegateCapability"]
