"""Model-visible creation and explicit promotion of generated capabilities."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from athena.affordances.models import AffordanceScope, DependencyRequirement
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_EFFECTS = {effect.value for effect in EffectClass}


class SynthesisCapability:
    """Create a generated tool through validation and the task overlay.

    This is intentionally a narrow admission API. It never edits the global
    registry or trusts the requested effect declaration as authority. Generated
    code is validated by :class:`SynthesisEngine`; creation is task-local and
    promotion is explicit, then both are invoked through the dispatcher like
    every other capability.
    """

    descriptor = CapabilityDescriptor(
        id="synthesis",
        description=(
            "Create a task-local deterministic capability from Python source, "
            "or explicitly promote/deprecate a validated tool. "
            "The source is sandbox-validated before registration. Operations: "
            "create/promote/deprecate."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {"type": "string", "enum": ["create", "promote", "deprecate"]},
                "name": {"type": "string", "pattern": _NAME.pattern},
                "description": {"type": "string", "minLength": 1, "maxLength": 1000},
                "code": {"type": "string", "minLength": 1, "maxLength": 200_000},
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "effects": {
                    "type": "array", "items": {"type": "string", "enum": sorted(_EFFECTS)},
                    "uniqueItems": True,
                },
                "validation_cases": {
                    "type": "array", "minItems": 1, "maxItems": 100,
                    "items": {"type": "object"},
                },
                "validation_tier": {
                    "type": "string",
                    "enum": ["scratch", "task", "candidate", "project", "user"],
                    "default": "task",
                },
                "capability_id": {"type": "string", "minLength": 1},
                "scope": {"type": "string", "enum": ["project", "user"]},
                "required_dependencies": {
                    "type": "array", "maxItems": 64,
                    "items": {
                        "type": "object", "required": ["name"],
                        "properties": {
                            "name": {"type": "string", "minLength": 1, "maxLength": 128},
                            "manager": {"type": "string", "enum": ["python"]},
                            "version": {"type": "string", "maxLength": 64},
                            "reason": {"type": "string", "maxLength": 1000},
                            "required_for": {"type": "string", "maxLength": 256},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            "oneOf": [
                {"properties": {"operation": {"const": "create"}},
                 "required": ["name", "description", "code", "validation_cases"]},
                {"properties": {"operation": {"const": "promote"}},
                 "required": ["capability_id", "scope"]},
                {"properties": {"operation": {"const": "deprecate"}},
                 "required": ["capability_id"]},
            ],
            "additionalProperties": False,
        },
        effects=frozenset({
            EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS, EffectClass.WRITE_LOCAL,
        }),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, engine, fabric) -> None:
        self._engine = engine
        self._fabric = fabric

    async def invoke(self, request: CapabilityRequest, *, context=None, **kw):
        if request.task_id is None:
            return _result(request, ok=False,
                           error="generated capabilities require a task scope")
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")
        if operation == "deprecate":
            workspace = getattr(context, "workspace", None)
            try:
                deprecated = await self._fabric.deprecate(
                    str(args.get("capability_id") or ""),
                    task_id=request.task_id,
                    project_id=getattr(workspace, "id", None),
                    user_id="athena",
                    scope=args.get("scope"),
                )
            except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                return _result(request, ok=False, error=str(exc))
            if not deprecated:
                return _result(request, ok=False, error="capability is unknown, not owned, or already deprecated")
            return _result(request, output=json.dumps({
                "capability_id": args["capability_id"],
                "status": "deprecated",
            }))
        if operation == "promote":
            if context is None:
                return _result(request, ok=False,
                               error="promotion requires workspace context")
            scope = str(args.get("scope") or "")
            project_id = context.workspace.id if scope == "project" else None
            user_id = "athena" if scope == "user" else ""
            try:
                promoted = self._engine.promote(
                    self._fabric, str(args.get("capability_id") or ""),
                    scope=AffordanceScope(scope),
                    project_id=project_id,
                    user_id=user_id,
                )
                if promoted:
                    # Promotion is not acknowledged until the durable
                    # definition has reached the store.
                    await self._fabric.flush()
            except (TypeError, ValueError) as exc:
                return _result(request, ok=False, error=str(exc))
            except RuntimeError as exc:
                return _result(request, ok=False, error=f"promotion persistence failed: {exc}")
            if not promoted:
                return _result(request, ok=False,
                               error="capability is not validated or unknown")
            return _result(request, output=json.dumps({
                "capability_id": args["capability_id"], "scope": scope,
                "project_id": project_id, "user_id": user_id or None,
            }), metadata={"capability_id": args["capability_id"], "scope": scope})
        name = str(args.get("name") or "").strip()
        if not _NAME.fullmatch(name):
            return _result(request, ok=False, error="invalid generated capability name")

        validation_cases = [dict(case) for case in args["validation_cases"]]
        input_schema_arg = args.get("input_schema")
        input_schema = (
            dict(input_schema_arg)
            if input_schema_arg is not None
            else infer_input_schema(validation_cases)
        )
        output_schema_arg = args.get("output_schema")
        output_schema = (
            dict(output_schema_arg) if output_schema_arg is not None else None
        )
        required_dependencies = tuple(
            DependencyRequirement(
                name=str(dependency["name"]),
                manager=str(dependency.get("manager") or "python"),
                version=dependency.get("version"),
                reason=str(dependency.get("reason") or ""),
                required_for=dependency.get("required_for"),
            )
            for dependency in args.get("required_dependencies") or ()
        )

        cap = self._engine.synthesize(
            name=name,
            description=str(args.get("description") or ""),
            code=str(args.get("code") or ""),
            input_schema=input_schema,
            output_schema=output_schema,
            effects=set(args.get("effects") or {EffectClass.READ_LOCAL.value}),
            task_id=request.task_id,
            provenance={
                "origin": "model_synthesis",
                "task_id": request.task_id,
                "request_call_id": request.call_id,
            },
            validation_cases=validation_cases,
            required_dependencies=required_dependencies,
        )
        cap = await self._engine.validate(
            cap,
            validation_cases,
            tier=str(args.get("validation_tier") or "task"),
            workspace_root=(
                getattr(getattr(context, "workspace", None), "root", None)
                if context is not None else None
            ),
        )
        # Content-addressed IDs are assigned only after source formatting and
        # output-schema inference. The advertised contract is therefore part
        # of the identity, rather than a pre-validation model declaration.
        identity = json.dumps({
            "name": name,
            "code": cap.code,
            "input_schema": cap.input_schema,
            "output_schema": cap.output_schema or {},
            "effects": sorted(args.get("effects") or (EffectClass.READ_LOCAL.value,)),
            "required_dependencies": [
                dependency.__dict__ for dependency in required_dependencies
            ],
        }, sort_keys=True, separators=(",", ":")).encode()
        cap.id = "synth_" + hashlib.sha256(identity).hexdigest()[:20]
        if not cap.validation.get("all_passed"):
            return _result(
                request, ok=False,
                error="generated capability validation failed",
                output=json.dumps({"capability_id": cap.id, "validation": cap.validation}),
                metadata={"capability_id": cap.id, "validation": cap.validation},
            )
        try:
            admitted = self._engine.register_ephemeral(self._fabric, cap)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return _result(request, ok=False, error=f"generated capability admission failed: {exc}")
        if not admitted:
            return _result(request, ok=False, error="generated capability was not admitted")
        proof = self._engine.proof_for(cap.id) or {}
        return _result(
            request,
            output=json.dumps({"capability_id": cap.id, "proof": proof}),
            metadata={"capability_id": cap.id, "proof": proof, "scope": "task"},
        )


def _result(request, *, ok: bool = True, output: str = "", error: str | None = None,
            metadata: dict[str, Any] | None = None) -> CapabilityResult:
    return CapabilityResult(
        request.call_id, request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output, error=error, metadata=dict(metadata or {}),
    )


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
        "properties": {
            key: _merge_value_schemas(values) for key, values in properties.items()
        },
        "required": [
            key for key in properties
            if all(key in argument_set for argument_set in arguments)
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
        properties = {
            str(key): _schema_for_value(item) for key, item in value.items()
        }
        return {
            "type": "object", "properties": properties,
            "required": sorted(properties), "additionalProperties": False,
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return {
            "type": "array",
            "items": _merge_value_schemas(list(value)) if value else {},
        }
    return {}
