"""``skills`` capability (thin wrapper).

Exposes skill search/trigger to the model. Delegates to an injected skills
loader/selector handle (built elsewhere). Effects: READ_LOCAL for search,
EXECUTE for trigger.
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

_INPUT_SCHEMA = {
    "type": "object",
    "required": ["operation"],
    "properties": {
        "operation": {"type": "string", "enum": ["search", "trigger"]},
        "query": {"type": "string"},
        "skill_id": {"type": "string"},
        "arguments": {"type": "object"},
    },
}


class SkillsCapability:
    descriptor = CapabilityDescriptor(
        id="skills",
        description=(
            "Skills: search the installed skill library, or trigger a skill by "
            "id. Delegates to the skills loader/selector."
        ),
        input_schema=_INPUT_SCHEMA,
        effects=frozenset({EffectClass.READ_LOCAL, EffectClass.EXECUTE}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, skills_store=None) -> None:
        self.skills_store = skills_store

    async def invoke(
        self, request: CapabilityRequest, *, context=None, **kwargs
    ) -> CapabilityResult:
        # Skills currently resolve through their injected store, but all
        # capabilities receive the dispatcher context uniformly.
        del context, kwargs
        args = request.arguments or {}
        op = args.get("operation", "search")
        call_id = request.call_id or new_id("call")
        if self.skills_store is None:
            return CapabilityResult(
                call_id, request.capability_id,
                CapabilityResultStatus.FAILED,
                error="skills store not available",
            )
        if op in ("search", "select"):
            query = args.get("query") or args.get("objective") or ""
            matches = await self.skills_store.search(query=query)
            return CapabilityResult(
                call_id, request.capability_id, CapabilityResultStatus.OK,
                output=str(matches),
            )
        if op == "trigger":
            outcome = await self.skills_store.trigger(
                skill_id=args.get("skill_id"), arguments=args.get("arguments") or {}
            )
            return CapabilityResult(
                call_id, request.capability_id, CapabilityResultStatus.OK,
                output=str(outcome),
            )
        return CapabilityResult(
            call_id, request.capability_id, CapabilityResultStatus.FAILED,
            error=f"unknown operation: {op}",
        )


__all__ = ["SkillsCapability"]
