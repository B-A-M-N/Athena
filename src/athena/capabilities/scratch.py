"""Task-local scratch computation.

Scratch is the cheap OI-style escape hatch: generate a small deterministic
helper, run it once in the restricted generated-code backend, and feed the
structured observation back to the same task. It deliberately does not
register a capability or persist source. Callers that discover repeated value
should use synthesis to create a validated, reusable affordance.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from athena.affordances.scratch import ScratchManager
from athena.capabilities.synthesis import infer_input_schema
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)


class ScratchCapability:
    """Run one generated deterministic helper without promoting it."""

    descriptor = CapabilityDescriptor(
        id="scratch",
        description=(
            "Run a short task-local deterministic Python helper in the "
            "restricted generated-code sandbox and return structured output. "
            "Scratch is not registered or retained; use synthesis for a "
            "reusable capability. Operation: run."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {"type": "string", "const": "run"},
                "code": {"type": "string", "minLength": 1, "maxLength": 200_000},
                "scratch_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "args": {"type": "object"},
                "purpose": {"type": "string", "maxLength": 1000},
            },
            "oneOf": [
                {"required": ["code"]},
                {"required": ["scratch_id"]},
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
    )

    def __init__(self, engine, scratch: ScratchManager, fabric=None) -> None:
        self._engine = engine
        self._scratch = scratch
        self._fabric = fabric

    async def invoke(self, request: CapabilityRequest, *, context=None, **kwargs):
        if request.task_id is None:
            return _result(request, ok=False, error="scratch requires a task scope")
        args = dict(request.arguments or {})
        scratch_id = str(args.get("scratch_id") or "")
        if scratch_id:
            try:
                program = self._scratch.get(scratch_id, task_id=request.task_id)
            except KeyError:
                return _result(request, ok=False, error="unknown scratch_id")
            code = program.code
        else:
            code = str(args.get("code") or "")
        if not code:
            return _result(request, ok=False, error="code is required for a new scratch program")
        input_args = args.get("args") or {}
        if not isinstance(input_args, Mapping):
            return _result(request, ok=False, error="args must be an object")

        if not scratch_id:
            program = self._scratch.create(
                code=code,
                task_id=request.task_id,
                purpose=str(args.get("purpose") or ""),
            )
        # A single scratch invocation still gets a real, strict contract. It
        # is derived from the invocation shape and returned as metadata so the
        # kernel can promote the exact operation later without reconstructing
        # the schema from prose.
        input_schema = infer_input_schema([{"args": dict(input_args)}])
        program = self._scratch.set_contract(
            program.id,
            input_schema=input_schema,
        )
        cap = self._engine.synthesize(
            capability_id=program.id,
            name=program.id,
            description=str(args.get("purpose") or "scratch computation"),
            code=code,
            input_schema=input_schema,
            task_id=request.task_id,
            provenance={
                "origin": "scratch",
                "task_id": request.task_id,
                "request_call_id": request.call_id,
            },
        )
        cap = await self._engine.validate(
            cap,
            [{"args": dict(input_args)}],
            tier="scratch",
            workspace_root=(
                getattr(getattr(context, "workspace", None), "root", None)
                if context is not None
                else None
            ),
            workspace=getattr(context, "workspace", None),
            task_id=request.task_id,
            session_id=request.session_id,
            profile=getattr(context, "autonomy", None),
            task_policy=getattr(context, "capability_policy", None),
            task_budget=getattr(context, "resource_budget", None),
            generated_call_depth=getattr(context, "generated_call_depth", 0),
            generated_call_chain=tuple(getattr(context, "generated_call_chain", ())),
        )
        passed = bool(cap.validation.get("all_passed"))
        detail = (cap.validation.get("details") or [{}])[0]
        value = detail.get("value") if isinstance(detail, dict) else None
        output = json.dumps(value) if passed else ""
        error = (
            None
            if passed
            else str(
                detail.get("error") if isinstance(detail, dict) else "scratch validation failed"
            )
        )
        self._scratch.record_result(
            program.id,
            ok=passed,
            output=output,
            error=error,
            arguments=dict(input_args),
        )
        computation = self._scratch.computation_record(program.id)
        promotion: dict[str, Any] | None = None
        if passed and self._fabric is not None and self._scratch.promotion_ready(program.id):
            cases = self._scratch.validation_cases(program.id)
            reusable = self._engine.synthesize(
                capability_id=program.id,
                name=program.id,
                description=str(args.get("purpose") or "repeated scratch computation"),
                code=program.code,
                input_schema=infer_input_schema(cases),
                task_id=request.task_id,
                provenance={
                    "origin": "scratch_auto_elevation",
                    "task_id": request.task_id,
                    "scratch_id": program.id,
                },
                validation_cases=cases,
            )
            reusable = await self._engine.validate(
                reusable,
                cases,
                tier="task",
                workspace_root=(
                    getattr(getattr(context, "workspace", None), "root", None)
                    if context is not None
                    else None
                ),
                workspace=getattr(context, "workspace", None),
                task_id=request.task_id,
                session_id=request.session_id,
                profile=getattr(context, "autonomy", None),
                task_policy=getattr(context, "capability_policy", None),
                task_budget=getattr(context, "resource_budget", None),
                generated_call_depth=getattr(context, "generated_call_depth", 0),
                generated_call_chain=tuple(getattr(context, "generated_call_chain", ())),
            )
            if reusable.validation.get("all_passed"):
                if self._engine.register_ephemeral(self._fabric, reusable):
                    promotion = {
                        "capability_id": reusable.id,
                        "status": "task_reusable",
                        "evidence": self._engine.proof_for(reusable.id),
                    }
        return _result(
            request,
            ok=passed,
            output=output,
            error=error if not passed else None,
            metadata={
                "scratch_id": program.id,
                "input_schema": input_schema,
                "computation": computation.to_record(),
                "validation": cap.validation,
                **({"promotion": promotion} if promotion else {}),
            },
        )


def _result(
    request,
    *,
    ok: bool,
    output: str = "",
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> CapabilityResult:
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
        metadata=dict(metadata or {}),
    )


__all__ = ["ScratchCapability"]
