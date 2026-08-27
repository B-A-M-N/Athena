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
            "required": ["operation", "code"],
            "properties": {
                "operation": {"type": "string", "const": "run"},
                "code": {"type": "string", "minLength": 1, "maxLength": 200_000},
                "args": {"type": "object"},
                "purpose": {"type": "string", "maxLength": 1000},
            },
            "additionalProperties": False,
        },
        effects=frozenset({
            EffectClass.READ_LOCAL,
            EffectClass.EXECUTE,
            EffectClass.SPAWN_PROCESS,
        }),
        origin=CapabilityOrigin.GENERATED,
    )

    def __init__(self, engine, scratch: ScratchManager) -> None:
        self._engine = engine
        self._scratch = scratch

    async def invoke(self, request: CapabilityRequest, *, context=None, **kwargs):
        if request.task_id is None:
            return _result(request, ok=False, error="scratch requires a task scope")
        args = dict(request.arguments or {})
        code = str(args.get("code") or "")
        input_args = args.get("args") or {}
        if not isinstance(input_args, Mapping):
            return _result(request, ok=False, error="args must be an object")

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
                if context is not None else None
            ),
        )
        passed = bool(cap.validation.get("all_passed"))
        detail = (cap.validation.get("details") or [{}])[0]
        value = detail.get("value") if isinstance(detail, dict) else None
        output = json.dumps(value) if passed else ""
        error = None if passed else str(
            detail.get("error") if isinstance(detail, dict)
            else "scratch validation failed"
        )
        self._scratch.record_result(
            program.id, ok=passed, output=output, error=error
        )
        return _result(
            request,
            ok=passed,
            output=output,
            error=error if not passed else None,
            metadata={
                "scratch_id": program.id,
                "input_schema": input_schema,
                "validation": cap.validation,
            },
        )


def _result(request, *, ok: bool, output: str = "", error: str | None = None,
            metadata: dict[str, Any] | None = None) -> CapabilityResult:
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
        metadata=dict(metadata or {}),
    )


__all__ = ["ScratchCapability"]
