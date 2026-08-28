"""Model-visible creation and explicit promotion of generated capabilities."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from athena.affordances.models import (
    AffordanceScope,
    DependencyRequirement,
    EvidenceDependency,
)
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
            "or explicitly repair, promote, or deprecate a validated tool. "
            "The source is sandbox-validated before registration. Operations: "
            "create/repair/promote_scratch/candidates/inspect/promote/deprecate. Generated run(args) code may compose "
            "governed native tools with athena.call(capability_id, arguments); "
            "those calls remain policy- and RealityGate-checked."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "create",
                        "repair",
                        "promote_scratch",
                        "candidates",
                        "inspect",
                        "promote",
                        "deprecate",
                    ],
                },
                "name": {"type": "string", "pattern": _NAME.pattern},
                "description": {"type": "string", "minLength": 1, "maxLength": 1000},
                "code": {"type": "string", "minLength": 1, "maxLength": 200_000},
                "runtime": {
                    "type": "string",
                    "enum": [
                        "python",
                        "python_persistent",
                    ],
                },
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "effects": {
                    "type": "array",
                    "items": {"type": "string", "enum": sorted(_EFFECTS)},
                    "uniqueItems": True,
                },
                "validation_cases": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {
                        "type": "object",
                        "properties": {
                            "args": {"type": "object"},
                            "workspace_files": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                            },
                            "workspace": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                            },
                            "expect_output": {},
                            "expect_output_contains": {},
                            "expected_error": {},
                            "expect_failure": {"type": "boolean"},
                            "expect_error_contains": {"type": "string"},
                            "expect_effect": {},
                            "expect_effects": {"type": "array"},
                            "expect_no_effects": {"type": "array"},
                            "expected_changed_resources": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "expected_unchanged_resources": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "invariants": {"type": "array"},
                            "verification_requirements": {"type": "array"},
                        },
                        "additionalProperties": False,
                    },
                },
                "validation_tier": {
                    "type": "string",
                    "enum": ["scratch", "task", "candidate", "project", "user"],
                    "default": "task",
                },
                "capability_id": {"type": "string", "minLength": 1},
                "scratch_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "scope": {"type": "string", "enum": ["project", "user"]},
                "required_dependencies": {
                    "type": "array",
                    "maxItems": 64,
                    "items": {
                        "type": "object",
                        "required": ["name"],
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
                "required_capabilities": {
                    "type": "array",
                    "maxItems": 64,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                "evidence_dependencies": {
                    "type": "array",
                    "maxItems": 128,
                    "items": {
                        "type": "object",
                        "required": ["requirement"],
                        "properties": {
                            "requirement": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 1000,
                            },
                            "evidence_id": {"type": "string", "minLength": 1},
                            "source_id": {"type": "string", "minLength": 1},
                            "content_hash": {"type": "string", "minLength": 1},
                            "invalidation": {"type": "string", "maxLength": 256},
                        },
                        "additionalProperties": False,
                    },
                },
                "provenance": {
                    "type": "object",
                    "maxProperties": 32,
                },
            },
            "oneOf": [
                {
                    "properties": {"operation": {"const": "create"}},
                    "required": ["name", "description", "code", "validation_cases"],
                },
                {
                    "properties": {"operation": {"const": "repair"}},
                    "required": [
                        "capability_id",
                        "name",
                        "description",
                        "code",
                        "validation_cases",
                    ],
                },
                {"properties": {"operation": {"const": "candidates"}}},
                {"properties": {"operation": {"const": "inspect"}}, "required": ["capability_id"]},
                {
                    "properties": {"operation": {"const": "promote"}},
                    "required": ["capability_id", "scope"],
                },
                {
                    "properties": {"operation": {"const": "deprecate"}},
                    "required": ["capability_id"],
                },
                {
                    "properties": {"operation": {"const": "promote_scratch"}},
                    "required": ["scratch_id"],
                },
            ],
            "additionalProperties": False,
        },
        effects=frozenset(
            {
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
                EffectClass.WRITE_LOCAL,
            }
        ),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, engine, fabric, research_store=None, scratch=None) -> None:
        self._engine = engine
        self._fabric = fabric
        self._research = research_store
        self._scratch = scratch

    async def invoke(self, request: CapabilityRequest, *, context=None, **kw):
        if request.task_id is None:
            return _result(request, ok=False, error="generated capabilities require a task scope")
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")
        if operation == "promote_scratch":
            if self._scratch is None:
                return _result(request, ok=False, error="scratch promotion is unavailable")
            try:
                program = self._scratch.get(
                    str(args.get("scratch_id") or ""), task_id=request.task_id
                )
            except KeyError:
                return _result(request, ok=False, error="unknown scratch_id")
            if not self._scratch.promotion_ready(program.id):
                return _result(
                    request,
                    ok=False,
                    error="scratch computation needs two distinct successful inputs",
                )
            cases = self._scratch.validation_cases(program.id)
            cap = self._engine.synthesize(
                capability_id=program.id,
                name=program.id,
                description=str(program.provenance.get("purpose") or "scratch computation"),
                code=program.code,
                input_schema=dict(program.input_schema),
                output_schema=dict(program.output_schema or {}) or None,
                task_id=request.task_id,
                provenance={
                    "origin": "scratch_synthesis_proposal",
                    "task_id": request.task_id,
                    "scratch_id": program.id,
                },
                validation_cases=cases,
            )
            cap = await self._engine.validate(
                cap,
                cases,
                tier="task",
                workspace_root=getattr(getattr(context, "workspace", None), "root", None),
                workspace=getattr(context, "workspace", None),
                task_id=request.task_id,
                session_id=request.session_id,
                profile=getattr(context, "autonomy", None),
                task_policy=getattr(context, "capability_policy", None),
                task_budget=getattr(context, "resource_budget", None),
            )
            if not cap.validation.get("all_passed"):
                return _result(
                    request,
                    ok=False,
                    error="scratch synthesis validation failed",
                    output=json.dumps({"validation": cap.validation}),
                )
            if not self._engine.register_ephemeral(self._fabric, cap):
                return _result(request, ok=False, error="scratch synthesis admission failed")
            return _result(
                request,
                output=json.dumps(
                    {
                        "capability_id": cap.id,
                        "status": "task_reusable",
                        "proof": self._engine.proof_for(cap.id),
                    }
                ),
            )
        if operation == "candidates":
            candidates = await self._fabric.candidates_for(request.task_id)
            return _result(
                request,
                output=json.dumps(
                    [
                        {
                            "capability_id": candidate.id,
                            "name": candidate.name,
                            "description": candidate.description,
                            "scope": candidate.scope.value,
                            "lifecycle_state": candidate.lifecycle_state,
                            "proof": dict(candidate.proof_record),
                            "code_hash": candidate.code_hash,
                            "schema_hash": candidate.schema_hash,
                            "required_capabilities": list(candidate.required_capabilities),
                            "evidence_dependencies": [
                                dependency.to_record()
                                for dependency in candidate.evidence_dependencies
                            ],
                        }
                        for candidate in candidates
                    ]
                ),
            )
        if operation == "inspect":
            capability_id = str(args.get("capability_id") or "")
            candidate = await self._fabric.persisted_for(
                capability_id,
                task_id=request.task_id,
            )
            if candidate is None:
                candidate = self._engine.synthetic_for(capability_id)
            if candidate is None:
                return _result(request, ok=False, error="capability is unknown or not owned")
            if hasattr(candidate, "to_record"):
                value = candidate.to_record()
            else:
                value = self._engine._generated_record(
                    candidate,
                    scope=AffordanceScope.TASK,
                ).to_record()
            return _result(request, output=json.dumps(value))
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
                return _result(
                    request,
                    ok=False,
                    error="capability is unknown, not owned, or already deprecated",
                )
            return _result(
                request,
                output=json.dumps(
                    {
                        "capability_id": args["capability_id"],
                        "status": "deprecated",
                    }
                ),
            )
        if operation == "promote":
            if context is None:
                return _result(request, ok=False, error="promotion requires workspace context")
            capability_id = str(args.get("capability_id") or "")
            # Candidate records are intentionally not active overlays. If the
            # task was restarted, rehydrate the exact owned candidate just
            # long enough to run the explicit promotion flow.
            if self._engine.synthetic_for(capability_id) is None:
                try:
                    await self._fabric.flush()
                    candidate = await self._fabric.persisted_for(
                        capability_id, task_id=request.task_id
                    )
                    if candidate is not None and candidate.scope is AffordanceScope.CANDIDATE:
                        self._engine.restore_executor(
                            candidate,
                            proof_sink=getattr(self._fabric, "update_generated_proof", None),
                            workspace_root=context.workspace.root,
                        )
                except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                    return _result(request, ok=False, error=f"candidate restore failed: {exc}")
            scope = str(args.get("scope") or "")
            project_id = context.workspace.id if scope == "project" else None
            user_id = "athena" if scope == "user" else ""
            try:
                cap = self._engine.synthetic_for(capability_id)
                if cap is None:
                    return _result(
                        request,
                        ok=False,
                        error="capability is not validated or unknown",
                    )
                # Promotion changes lifetime and visibility. Re-run the
                # behavioral fixtures at the destination tier instead of
                # treating task-level evidence as project/user evidence.
                cap = await self._engine.validate(
                    cap,
                    list(cap.validation_cases or []),
                    tier=("project" if scope == "project" else "user"),
                    workspace_root=context.workspace.root,
                    workspace=context.workspace,
                    task_id=request.task_id,
                    session_id=request.session_id,
                    profile=getattr(context, "autonomy", None),
                    task_policy=getattr(context, "capability_policy", None),
                    task_budget=getattr(context, "resource_budget", None),
                    generated_call_depth=getattr(context, "generated_call_depth", 0),
                    generated_call_chain=tuple(getattr(context, "generated_call_chain", ())),
                )
                evidence = await self._engine.evidence_status(cap, self._research)
                cap.validation["evidence"] = evidence
                if evidence["status"] != "CURRENT":
                    return _result(
                        request,
                        ok=False,
                        error="promotion evidence is stale or unavailable",
                        output=json.dumps(
                            {
                                "capability_id": capability_id,
                                "evidence": evidence,
                            }
                        ),
                    )
                if not cap.validation.get("all_passed"):
                    return _result(
                        request,
                        ok=False,
                        error="promotion validation failed",
                        output=json.dumps({"validation": cap.validation}),
                    )
                promoted = self._engine.promote(
                    self._fabric,
                    capability_id,
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
                return _result(
                    request,
                    ok=False,
                    error=(
                        "capability is not validated, unknown, or "
                        "lacks diverse live promotion proof"
                    ),
                )
            return _result(
                request,
                output=json.dumps(
                    {
                        "capability_id": capability_id,
                        "scope": scope,
                        "project_id": project_id,
                        "user_id": user_id or None,
                    }
                ),
                metadata={"capability_id": args["capability_id"], "scope": scope},
            )
        if operation == "repair":
            target_id = str(args.get("capability_id") or "")
            workspace = getattr(context, "workspace", None)
            target = self._fabric.provenance(target_id)
            if target is None:
                return _result(
                    request,
                    ok=False,
                    error="repair target is unknown or has no generated provenance",
                )
            owner_allowed = (
                target.get("task_scope") == request.task_id
                or target.get("project_scope") == getattr(workspace, "id", None)
                or target.get("user_scope") == "athena"
            )
            if not owner_allowed:
                return _result(request, ok=False, error="repair target is not visible to this task")
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
        output_schema = dict(output_schema_arg) if output_schema_arg is not None else None
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
        evidence_dependencies = tuple(
            EvidenceDependency.from_record(dict(dependency))
            for dependency in args.get("evidence_dependencies") or ()
        )
        required_capabilities = tuple(
            sorted(
                {
                    str(capability).strip()
                    for capability in args.get("required_capabilities") or ()
                    if str(capability).strip()
                }
            )
        )

        target_id = str(args.get("capability_id") or "") if operation == "repair" else ""
        cap = self._engine.synthesize(
            name=name,
            description=str(args.get("description") or ""),
            code=str(args.get("code") or ""),
            runtime=str(args.get("runtime") or "python"),
            input_schema=input_schema,
            output_schema=output_schema,
            effects=set(args.get("effects") or {EffectClass.READ_LOCAL.value}),
            task_id=request.task_id,
            provenance={
                "origin": "model_synthesis",
                "task_id": request.task_id,
                "request_call_id": request.call_id,
                **(
                    dict(args.get("provenance") or {})
                    if isinstance(args.get("provenance"), Mapping)
                    else {}
                ),
            },
            validation_cases=validation_cases,
            required_dependencies=required_dependencies,
            required_capabilities=required_capabilities,
            evidence_dependencies=evidence_dependencies,
            supersedes=(target_id,) if target_id else (),
        )
        cap = await self._engine.validate(
            cap,
            validation_cases,
            tier=str(args.get("validation_tier") or "task"),
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
        evidence = await self._engine.evidence_status(cap, self._research)
        cap.validation["evidence"] = evidence
        # Content-addressed IDs are assigned only after source formatting and
        # output-schema inference. The advertised contract is therefore part
        # of the identity, rather than a pre-validation model declaration.
        identity = json.dumps(
            {
                "name": name,
                "code": cap.code,
                "runtime": cap.runtime,
                "input_schema": cap.input_schema,
                "output_schema": cap.output_schema or {},
                "effects": sorted(args.get("effects") or (EffectClass.READ_LOCAL.value,)),
                "required_dependencies": [
                    dependency.__dict__ for dependency in required_dependencies
                ],
                "required_capabilities": list(cap.required_capabilities),
                "supersedes": list(cap.supersedes),
                "evidence_dependencies": [
                    dependency.to_record() for dependency in evidence_dependencies
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        cap.id = "synth_" + hashlib.sha256(identity).hexdigest()[:20]
        if evidence["status"] != "CURRENT":
            return _result(
                request,
                ok=False,
                error="generated capability evidence is stale or unavailable",
                output=json.dumps(
                    {
                        "capability_id": cap.id,
                        "evidence": evidence,
                    }
                ),
                metadata={"capability_id": cap.id, "evidence": evidence},
            )
        if not cap.validation.get("all_passed"):
            return _result(
                request,
                ok=False,
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
            metadata={
                "capability_id": cap.id,
                "proof": proof,
                "scope": "task",
                **({"supersedes": list(cap.supersedes)} if cap.supersedes else {}),
            },
        )


def _result(
    request,
    *,
    ok: bool = True,
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
