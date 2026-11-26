"""Model-visible creation and explicit promotion of generated capabilities."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from athena.capabilities.synthesis_descriptor import build_synthesis_descriptor
from athena.capabilities.synthesis_admission import invoke_synthesis
from athena.capabilities.synthesis_operations import (
    list_candidates,
    promote_capability,
    promote_scratch_capability,
)
from athena.protocol.capabilities import (
    CapabilityRequest,
)


class SynthesisCapability:
    """Create a generated tool through validation and the task overlay.

    This is intentionally a narrow admission API. It never edits the global
    registry or trusts the requested effect declaration as authority. Generated
    code is validated by :class:`SynthesisEngine`; creation is task-local and
    promotion is explicit, then both are invoked through the dispatcher like
    every other capability.
    """

    descriptor = build_synthesis_descriptor()

    def __init__(self, engine, fabric, research_store=None, scratch=None) -> None:
        self._engine = engine
        self._fabric = fabric
        self._research = research_store
        self._scratch = scratch

    async def invoke(self, request: CapabilityRequest, *, context=None, **kw):
        return await invoke_synthesis(self, request, context=context, **kw)

    async def _op_promote_scratch(self, request, args, context):
        return await promote_scratch_capability(self, request, args, context)

    async def _op_candidates(self, request):
        return await list_candidates(self, request)

    async def _op_promote(self, request, args, context):
        return await promote_capability(self, request, args, context)


__all__ = ["SynthesisCapability"]


def infer_input_schema(validation_cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Derive a strict object contract from positive validation fixtures.

    ``run(args)`` deliberately receives one object, so when a caller omits a
    schema we still manufacture a real contract instead of falling back to an
    unconstrained ``{}`` schema.  Fields present in every fixture are required;
    fields seen only in some fixtures are optional.  Future calls with unknown
    fields are rejected until the capability is regenerated with a new schema.
    """
    arguments = [dict(case.get("args") or {}) for case in validation_cases]
    if not arguments:
        raise ValueError("at least one validation case is required")
    properties: dict[str, list[Any]] = {}
    for argument_set in arguments:
        for key, value in argument_set.items():
            properties.setdefault(str(key), []).append(value)
    return {
        "type": "object",
        "properties": {key: _merge_value_schemas(values) for key, values in properties.items()},
        "required": [
            key for key in properties if all(key in argument_set for argument_set in arguments)
        ],
        "additionalProperties": False,
    }


def _merge_value_schemas(values: Sequence[Any]) -> dict[str, Any]:
    schemas = [_schema_for_value(value) for value in values]
    unique = {json.dumps(schema, sort_keys=True) for schema in schemas}
    if len(unique) == 1:
        return schemas[0]
    return {"anyOf": schemas}


def _schema_for_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, Mapping):
        properties = {str(key): _schema_for_value(item) for key, item in value.items()}
        return {
            "type": "object",
            "properties": properties,
            "required": sorted(properties),
            "additionalProperties": False,
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return {
            "type": "array",
            "items": _merge_value_schemas(list(value)) if value else {},
        }
    return {}
