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
    "additionalProperties": False,
    "properties": {
        "operation": {"type": "string", "enum": list(_OPERATIONS)},
        "objective": {"type": "string", "minLength": 1, "maxLength": 10000},
        "child_task_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "timeout": {"type": "number", "minimum": 0, "maximum": 3600},
        "metadata": {"type": "object", "maxProperties": 32},
        "context": {
            "type": "array", "maxItems": 64,
            "items": {
                "type": "object",
                "required": ["kind", "ref"],
                "properties": {
                    "kind": {"type": "string", "enum": [
                        "session", "memory", "skill", "artifact",
                        "file", "task", "web",
                    ]},
                    "ref": {"type": "string", "minLength": 1, "maxLength": 2048},
                    "source_id": {"type": "string", "maxLength": 256},
                    "summary": {"type": "string", "maxLength": 2000},
                    "mime_type": {"type": "string", "maxLength": 256},
                },
                "additionalProperties": False,
            },
        },
    },
    "oneOf": [
        {"properties": {"operation": {"const": "spawn"}},
         "required": ["objective"]},
        {"properties": {"operation": {"enum": ["status", "collect", "cancel"]}},
         "required": ["child_task_id"]},
    ],
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
        if not request.task_id:
            return CapabilityResult(
                call_id, request.capability_id,
                CapabilityResultStatus.FAILED,
                error="delegation requires an owning task",
            )
        try:
            if operation == "spawn":
                return await self._spawn(request, call_id, args)
            if operation == "status":
                return await self._status(request, args, call_id)
            if operation == "collect":
                return await self._collect(request, args, call_id)
            if operation == "cancel":
                return await self._cancel(request, args, call_id)
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
            parent_task_id=request.task_id,
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

    async def _status(
        self, request: CapabilityRequest, args: dict, call_id: str
    ) -> CapabilityResult:
        child_id = args.get("child_task_id")
        if not child_id:
            return CapabilityResult(
                call_id, self.descriptor.id, CapabilityResultStatus.FAILED,
                error="delegate.status requires child_task_id",
            )
        if not await self._owns_child(request.task_id, child_id):
            return self._ownership_failure(call_id, child_id)
        status = await self._handle.status_of(child_id)
        return CapabilityResult(
            call_id, self.descriptor.id, CapabilityResultStatus.OK,
            output=f"child {child_id} status: {status.value}",
            ref_uri=f"task:{child_id}",
            metadata={"operation": "status", "child_task_id": child_id,
                     "status": status.value},
        )

    async def _collect(
        self, request: CapabilityRequest, args: dict, call_id: str
    ) -> CapabilityResult:
        child_id = args.get("child_task_id")
        if not child_id:
            return CapabilityResult(
                call_id, self.descriptor.id, CapabilityResultStatus.FAILED,
                error="delegate.collect requires child_task_id",
            )
        if not await self._owns_child(request.task_id, child_id):
            return self._ownership_failure(call_id, child_id)
        timeout = args.get("timeout")
        result = await self._handle.collect(
            child_id, timeout=timeout if timeout is None else float(timeout),
        )
        status = _result_status(result)
        return CapabilityResult(
            call_id, self.descriptor.id, status,
            output=_format_result(result),
            ref_uri=f"task:{child_id}",
            metadata={
                "operation": "collect",
                "child_task_id": child_id,
                "status": result.status.value,
                "summary": result.summary,
            },
            error=(f"child ended with status {result.status.value}"
                   if status is CapabilityResultStatus.FAILED
                   and result.status in {TaskStatus.FAILED, TaskStatus.PARTIAL}
                   else (f"child is not complete (status={result.status.value})"
                         if status is CapabilityResultStatus.FAILED else None)),
        )

    async def _cancel(
        self, request: CapabilityRequest, args: dict, call_id: str
    ) -> CapabilityResult:
        child_id = args.get("child_task_id")
        if not child_id:
            return CapabilityResult(
                call_id, self.descriptor.id, CapabilityResultStatus.FAILED,
                error="delegate.cancel requires child_task_id",
            )
        if not await self._owns_child(request.task_id, child_id):
            return self._ownership_failure(call_id, child_id)
        status = await self._handle.cancel_child(child_id)
        return CapabilityResult(
            call_id, self.descriptor.id, CapabilityResultStatus.OK,
            output=f"cancelled child {child_id}",
            ref_uri=f"task:{child_id}",
            metadata={"operation": "cancel", "child_task_id": child_id,
                     "status": status.value},
        )

    async def _owns_child(self, parent_task_id: str | None, child_id: str) -> bool:
        checker = getattr(self._handle, "is_descendant", None)
        if checker is None or not parent_task_id:
            return False
        return bool(await checker(parent_task_id, child_id))

    def _ownership_failure(self, call_id: str, child_id: str) -> CapabilityResult:
        return CapabilityResult(
            call_id, self.descriptor.id, CapabilityResultStatus.FAILED,
            error=f"child task {child_id} is not owned by the requesting task",
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
                    mime_type=item.get("mime_type"),
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
    if result.status == TaskStatus.COMPLETE:
        return CapabilityResultStatus.OK
    return CapabilityResultStatus.FAILED


__all__ = ["DelegateCapability"]
