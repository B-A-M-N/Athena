"""Generated observers: deterministic normalizers for ugly machine output.

An observer is a generated capability with a deliberately small public
contract: it receives ``{"input": <raw observation>}`` and returns a
structured JSON value.  Source validation, sandbox execution, host-call
mediation, provenance, and lifecycle all remain owned by synthesis/fabric;
this capability only gives that machinery an observation-specific surface.
"""

from __future__ import annotations

import json
from dataclasses import replace
from collections.abc import Mapping
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


class ObserverCapability:
    descriptor = CapabilityDescriptor(
        id="observer",
        description=(
            "Compile and run deterministic generated observers that turn raw "
            "logs, compiler output, terminal text, or machine measurements "
            "into structured observations. The generated body receives "
            "args['input']; validation and sandbox rules are inherited from "
            "synthesis. Operations: create/run/list/inspect."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "create",
                        "run",
                        "list",
                        "inspect",
                    ],
                },
                "name": {"type": "string", "minLength": 1, "maxLength": 64},
                "description": {"type": "string", "minLength": 1, "maxLength": 1000},
                "code": {"type": "string", "minLength": 1, "maxLength": 200_000},
                "validation_cases": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {"type": "object"},
                },
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "observer_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "input": {},
                "input_kind": {"type": "string", "maxLength": 128},
                "output_kind": {"type": "string", "maxLength": 128},
            },
            "oneOf": [
                {
                    "properties": {"operation": {"const": "create"}},
                    "required": ["name", "description", "code", "validation_cases"],
                },
                {
                    "properties": {"operation": {"const": "run"}},
                    "required": ["observer_id", "input"],
                },
                {"properties": {"operation": {"const": "list"}}},
                {"properties": {"operation": {"const": "inspect"}}, "required": ["observer_id"]},
            ],
            "additionalProperties": False,
        },
        effects=frozenset(
            {
                EffectClass.READ_LOCAL,
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
            }
        ),
        origin=CapabilityOrigin.GENERATED,
        tags=frozenset({"observation", "deterministic", "sensor"}),
    )

    def __init__(self, synthesis, fabric, dispatcher=None) -> None:
        self._synthesis = synthesis
        self._fabric = fabric
        self._dispatcher = dispatcher

    async def invoke(self, request: CapabilityRequest, *, context=None, **kw):
        if request.task_id is None:
            return _result(request, ok=False, error="observers require a task scope")
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")

        if operation == "create":
            cases = [dict(case) for case in args.get("validation_cases") or ()]
            normalized_cases = []
            for case in cases:
                values = dict(case)
                case_args = dict(values.get("args") or {})
                if "input" not in case_args:
                    return _result(
                        request,
                        ok=False,
                        error="observer validation cases require args.input",
                    )
                values["args"] = case_args
                normalized_cases.append(values)
            forwarded = {
                "operation": "create",
                "name": str(args.get("name") or ""),
                "description": str(args.get("description") or ""),
                "code": str(args.get("code") or ""),
                "validation_cases": normalized_cases,
                "input_schema": args.get("input_schema"),
                "output_schema": args.get("output_schema"),
                "provenance": {
                    "origin": "generated_observer",
                    "observer_input_kind": str(args.get("input_kind") or "raw"),
                    "observer_output_kind": str(args.get("output_kind") or "structured"),
                },
            }
            forwarded = {key: value for key, value in forwarded.items() if value is not None}
            result = await self._synthesis.invoke(
                CapabilityRequest(
                    capability_id="synthesis",
                    arguments=forwarded,
                    task_id=request.task_id,
                    session_id=request.session_id,
                    call_id=request.call_id,
                    origin=request.origin,
                ),
                context=context,
            )
            return replace(
                result,
                metadata={
                    **dict(result.metadata or {}),
                    "observer": True,
                    "input_kind": str(args.get("input_kind") or "raw"),
                    "output_kind": str(args.get("output_kind") or "structured"),
                },
            )

        observer_id = str(args.get("observer_id") or "")
        if operation == "list":
            records = [
                record
                for record in self._fabric.created_this_task(request.task_id)
                if (record.get("provenance") or {}).get("origin") == "generated_observer"
            ]
            return _result(request, output=json.dumps(records))

        if operation == "inspect":
            result = await self._synthesis.invoke(
                CapabilityRequest(
                    capability_id="synthesis",
                    arguments={"operation": "inspect", "capability_id": observer_id},
                    task_id=request.task_id,
                    session_id=request.session_id,
                    call_id=request.call_id,
                    origin=request.origin,
                ),
                context=context,
            )
            return result

        if operation != "run":
            return _result(request, ok=False, error=f"unknown operation: {operation}")

        try:
            executor = self._fabric.executor_for(
                observer_id,
                task_id=request.task_id,
                project_id=getattr(getattr(context, "workspace", None), "id", None),
                user_id="athena",
            )
        except Exception as exc:  # noqa: BLE001 - capability boundary
            return _result(request, ok=False, error=f"unknown observer: {exc}")
        generated_request = CapabilityRequest(
            capability_id=observer_id,
            arguments={"input": args.get("input")},
            task_id=request.task_id,
            session_id=request.session_id,
            call_id=new_id("observer-run"),
            origin=CapabilityRequestOrigin.GENERATED,
        )
        if self._dispatcher is not None:
            workspace = getattr(context, "workspace", None)
            if workspace is None:
                return _result(
                    request, ok=False, error="observer execution requires workspace context"
                )
            result = await self._dispatcher.dispatch(
                generated_request,
                workspace=workspace,
                profile=getattr(context, "autonomy", None),
                task_policy=getattr(context, "capability_policy", None),
                task_budget=getattr(context, "resource_budget", None),
                _generated_call_depth=getattr(context, "generated_call_depth", 0) + 1,
                _generated_call_chain=(
                    *tuple(getattr(context, "generated_call_chain", ())),
                    observer_id,
                ),
            )
            # A generated observer cannot park an approval while its parent
            # call is still executing. The dispatcher normally turns that
            # into a result for native callers; keep the boundary explicit if
            # a custom dispatcher returns its suspension object directly.
            if not isinstance(result, CapabilityResult):
                return _result(request, ok=False, error="observer execution suspended")
        else:
            # Compatibility for isolated unit users that construct this
            # capability without a service dispatcher. The live service always
            # injects the canonical dispatcher above.
            result = await executor.invoke(generated_request, context=context)
        metadata = {
            **dict(result.metadata or {}),
            "observer": True,
            "observer_id": observer_id,
            "observation": result.output if result.status is CapabilityResultStatus.OK else None,
        }
        return replace(result, capability_id=request.capability_id, metadata=metadata)


def _result(
    request,
    *,
    ok: bool = True,
    output: str = "",
    error: str | None = None,
    metadata: Mapping[str, Any] | None = None,
):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
        metadata=dict(metadata or {}),
    )


__all__ = ["ObserverCapability"]
