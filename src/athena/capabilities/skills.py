"""``skills`` capability (thin wrapper).

Exposes skill search/trigger to the model. Delegates to an injected skills
loader/selector handle (built elsewhere). Effects: READ_LOCAL for search,
EXECUTE for trigger.
"""

from __future__ import annotations

import json

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
    "additionalProperties": False,
    "properties": {
        "operation": {"type": "string", "enum": ["search", "trigger"]},
        "query": {"type": "string", "maxLength": 2000},
        "skill_id": {"type": "string", "minLength": 1, "maxLength": 4096},
        "arguments": {"type": "object", "maxProperties": 64},
    },
    "oneOf": [
        {"properties": {"operation": {"const": "search"}}},
        {"properties": {"operation": {"const": "trigger"}},
         "required": ["skill_id"]},
    ],
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
            query = str(args.get("query") or "")
            matches = await self.skills_store.search(query=query, limit=10)
            return CapabilityResult(
                call_id, request.capability_id, CapabilityResultStatus.OK,
                output=json.dumps([_skill_record(item) for item in matches], sort_keys=True),
            )
        if op == "trigger":
            outcome = await self.skills_store.trigger(
                skill_id=args.get("skill_id"), arguments=args.get("arguments") or {},
                task_id=request.task_id,
            )
            return CapabilityResult(
                call_id, request.capability_id, CapabilityResultStatus.OK,
                output=json.dumps(_skill_record(outcome), sort_keys=True),
            )
        return CapabilityResult(
            call_id, request.capability_id, CapabilityResultStatus.FAILED,
            error=f"unknown operation: {op}",
        )


def _skill_record(skill) -> dict:
    if isinstance(skill, dict):
        return dict(skill)
    return {
        "id": getattr(skill, "id", ""),
        "name": getattr(skill, "name", ""),
        "description": getattr(skill, "description", ""),
        "body": getattr(skill, "body", ""),
        "triggers": list(getattr(skill, "triggers", ()) or ()),
        "scope": getattr(skill, "scope", ""),
        "trust": getattr(getattr(skill, "trust", None), "value", getattr(skill, "trust", "")),
        "version": getattr(skill, "version", 1),
        "enabled": bool(getattr(skill, "enabled", True)),
        "metadata": dict(getattr(skill, "metadata", {}) or {}),
    }


__all__ = ["SkillsCapability"]
