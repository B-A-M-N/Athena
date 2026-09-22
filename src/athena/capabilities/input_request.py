"""``request_input`` capability descriptor.

The model calls this when required information is missing and guessing would
be wrong or destructive. The AgentKernel intercepts the call before any
dispatcher sees it — the registered executor below is a truthful fallback that
should never run on a healthy path — parks the task in ``WAITING_INPUT`` with
the question durable, and the operator's answer resumes the SAME task. The
descriptor exists so the model can discover and shape the call; the kernel
owns the semantics.
"""

from __future__ import annotations
from athena.capabilities.operations import native_descriptor

from athena.protocol.capabilities import (
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)

INPUT_REQUEST_DESCRIPTOR = native_descriptor(
    id="request_input",
    description=(
        "Ask the operator a clarifying question when required information is "
        "missing. Use this instead of guessing when the answer changes what "
        "work should happen; the task pauses and resumes with their answer."
    ),
    input_schema={
        "type": "object",
        "required": ["question"],
        "additionalProperties": False,
        "properties": {
            "question": {"type": "string", "minLength": 1, "maxLength": 4000},
            "choices": {
                "type": "array",
                "maxItems": 16,
                "items": {"type": "string", "maxLength": 500},
            },
            "expected": {
                "type": "string",
                "enum": ["text", "choice", "confirmation", "path", "secret"],
                "description": "What kind of answer is expected.",
            },
            "context": {
                "type": "object",
                "maxProperties": 8,
                "description": "Bounded context for the operator UI.",
            },
        },
    },
    effects=frozenset({EffectClass.READ_LOCAL}),
    tags=frozenset({"clarification", "question", "operator", "input"}),
    origin=CapabilityOrigin.NATIVE,
)


class InputRequestCapability:
    """Descriptor-only affordance; the kernel owns the call semantics."""

    descriptor = INPUT_REQUEST_DESCRIPTOR

    async def invoke(self, request: CapabilityRequest, **kw) -> CapabilityResult:
        del kw
        return CapabilityResult(
            request.call_id,
            self.descriptor.id,
            CapabilityResultStatus.FAILED,
            error="request_input must be handled by the agent kernel",
        )


__all__ = ["INPUT_REQUEST_DESCRIPTOR", "InputRequestCapability"]
