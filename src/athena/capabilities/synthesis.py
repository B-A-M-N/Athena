"""Model-visible creation and explicit promotion of generated capabilities."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
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
    CapabilityRequestOrigin,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
    ResourceClass,
)

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_EFFECTS = {effect.value for effect in EffectClass}


def _fixture_hash(case: Mapping[str, Any]) -> str:
    """Hash behavioral fixture content, excluding live-evidence annotations."""
    comparable = {
        key: value
        for key, value in case.items()
        if key
        not in {
            "id",
            "source",
            "failure_class",
            "observed_failure",
            "resolved_by_revision",
            "capability_family",
            "revision_first_seen",
            "environment_fingerprint",
            "expected_contract",
            # RegressionCase carries both names for replay compatibility.
            # Normalize them before hashing so an inherited live failure and
            # an authored fixture for the same input cannot execute twice.
            "args",
            "input",
        }
    }
    if "input" in case:
        comparable["args"] = case["input"]
    elif "args" in case:
        comparable["args"] = case["args"]
    return hashlib.sha256(
        json.dumps(comparable, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _merge_validation_cases(*groups: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep the first copy of each deterministic fixture in corpus order."""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for raw_case in group:
            case = dict(raw_case)
            fingerprint = _fixture_hash(case)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            merged.append(case)
    return merged


def _merge_dependency_requirements(
    *groups: Sequence[DependencyRequirement],
) -> tuple[DependencyRequirement, ...]:
    merged: dict[str, DependencyRequirement] = {}
    for group in groups:
        for dependency in group:
            merged[dependency.key()] = dependency
    return tuple(merged[key] for key in sorted(merged))


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
            "or explicitly repair, migrate, promote, or deprecate a validated tool. "
            "The source is sandbox-validated before registration. Operations: "
            "create/repair/revalidate/migrate_contract/promote_scratch/candidates/inspect/promote/deprecate. Generated run(args) code may compose "
            "governed native tools with athena.call(capability_id, arguments); "
            "those calls remain policy- and RealityGate-checked."
        ),
        tags=frozenset({"synthesis", "create", "generate", "repair", "construct"}),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "create",
                        "repair",
                        "revalidate",
                        "migrate_contract",
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
                            "expect_invalid_input": {"type": "boolean"},
                            "source": {"type": "string", "maxLength": 64},
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
                "compatibility": {
                    "type": "string",
                    "enum": ["backward_compatible", "breaking"],
                    "default": "backward_compatible",
                },
                "operator_confirmation": {"type": "boolean"},
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
                {
                    "properties": {"operation": {"const": "revalidate"}},
                    "required": ["capability_id"],
                },
                {
                    "properties": {"operation": {"const": "migrate_contract"}},
                    "required": [
                        "capability_id",
                        "name",
                        "description",
                        "code",
                        "input_schema",
                        "output_schema",
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
                EffectClass.READ_LOCAL,
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
                EffectClass.WRITE_LOCAL,
            }
        ),
        resources=frozenset({ResourceClass.SYNTHESIS}),
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
            # Scratch auto-elevates after its second distinct successful input.
            # An explicit synthesis.promote_scratch call is therefore allowed
            # to arrive after the task overlay already contains this exact
            # capability.  Reuse that live, validated overlay instead of
            # trying to install the same id twice into the fabric.
            existing = self._engine.synthetic_for(program.id)
            if (
                existing is not None
                and existing.task_id == request.task_id
                and self._fabric.has(program.id, task_id=request.task_id)
            ):
                return _result(
                    request,
                    output=json.dumps(
                        {
                            "capability_id": program.id,
                            "status": "task_reusable",
                            "proof": self._engine.proof_for(program.id),
                        }
                    ),
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
                promoted = await self._engine.promote(
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
            except (OSError, RuntimeError) as exc:
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
        if operation in {"repair", "revalidate", "migrate_contract"}:
            target_id = str(args.get("capability_id") or "")
            workspace = getattr(context, "workspace", None)
            target_cap = self._engine.synthetic_for(target_id)
            target_record = None
            if target_cap is None:
                # A candidate may outlive the process that created it. Resolve
                # the durable record through its owner boundary, then restore
                # the exact predecessor before accepting a repair request.
                try:
                    await self._fabric.flush()
                    for lookup in (
                        {"task_id": request.task_id},
                        {"project_id": getattr(workspace, "id", None)},
                        {"user_id": "athena"},
                    ):
                        if not any(lookup.values()):
                            continue
                        target_record = await self._fabric.persisted_for(target_id, **lookup)
                        if target_record is None:
                            continue
                        self._engine.restore_executor(
                            target_record,
                            proof_sink=getattr(self._fabric, "update_generated_proof", None),
                            workspace_root=getattr(workspace, "root", None),
                        )
                        target_cap = self._engine.synthetic_for(target_id)
                        break
                except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                    return _result(
                        request, ok=False, error=f"{operation} target restore failed: {exc}"
                    )
            target = self._fabric.provenance(target_id)
            if target is None and target_record is not None:
                target = target_record.to_record()
            if target is None or target_cap is None:
                return _result(
                    request,
                    ok=False,
                    error=f"{operation} target is unknown or has no generated provenance",
                )
            owner_allowed = (
                target.get("task_scope") == request.task_id
                or target.get("project_scope") == getattr(workspace, "id", None)
                or target.get("user_scope") == "athena"
            )
            if not owner_allowed:
                return _result(
                    request,
                    ok=False,
                    error=f"{operation} target is not visible to this task",
                )
            lifecycle_state = str(target.get("lifecycle_state") or "")
            blocked_states = {"SUPERSEDED", "DEPRECATED"}
            if operation != "revalidate":
                blocked_states |= {"STALE", "REJECTED", "REVALIDATION_REQUIRED"}
            if lifecycle_state in blocked_states:
                return _result(
                    request,
                    ok=False,
                    error=(
                        f"{operation} target is unavailable; use the active revision "
                        "or revalidate it before creating a successor"
                    ),
                )
        if operation == "revalidate":
            if target_cap is None:
                return _result(request, ok=False, error="revalidate target is unavailable")
            if context is None or getattr(context, "workspace", None) is None:
                return _result(request, ok=False, error="revalidation requires workspace context")
            original_state = target_cap.lifecycle_state
            original_identity = (
                target_cap.id,
                target_cap.code,
                dict(target_cap.input_schema),
                dict(target_cap.output_schema or {}),
                target_cap.revision,
                target_cap.parent_revision,
                target_cap.family_id,
            )
            all_cases = _merge_validation_cases(
                target_cap.validation_cases or [],
                target_cap.validation.get("regression_cases") or [],
                target_cap.validation.get("live_failure_cases") or [],
            )
            target_scope = (
                target_record.scope
                if target_record is not None
                else (
                    AffordanceScope.PROJECT if target_cap.task_id is None else AffordanceScope.TASK
                )
            )
            validation_tier = {
                AffordanceScope.PROJECT: "project",
                AffordanceScope.USER: "user",
                AffordanceScope.TASK: "task",
                AffordanceScope.CANDIDATE: "candidate",
            }[target_scope]
            target_cap = await self._engine.validate(
                target_cap,
                all_cases,
                tier=validation_tier,
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
            evidence = await self._engine.evidence_status(target_cap, self._research)
            target_cap.validation["evidence"] = evidence
            unchanged = original_identity == (
                target_cap.id,
                target_cap.code,
                dict(target_cap.input_schema),
                dict(target_cap.output_schema or {}),
                target_cap.revision,
                target_cap.parent_revision,
                target_cap.family_id,
            )
            if (
                not unchanged
                or not target_cap.validation.get("all_passed")
                or evidence["status"] != "CURRENT"
            ):
                target_cap.lifecycle_state = original_state
                return _result(
                    request,
                    ok=False,
                    error="generated capability revalidation failed",
                    output=json.dumps(
                        {
                            "capability_id": target_cap.id,
                            "unchanged_identity": unchanged,
                            "validation": target_cap.validation,
                            "evidence": evidence,
                        }
                    ),
                )
            target_cap.lifecycle_state = (
                "PROMOTED"
                if target_scope in {AffordanceScope.PROJECT, AffordanceScope.USER}
                else "CANDIDATE"
                if target_scope is AffordanceScope.CANDIDATE
                else "VALIDATED"
            )
            generated = self._engine._generated_record(
                target_cap,
                scope=target_scope,
                project_scope=(
                    target_record.project_scope
                    if target_record
                    else getattr(context.workspace, "id", None)
                ),
                user_scope=(target_record.user_scope if target_record else None),
            )
            if target_record is not None:
                generated = replace(
                    generated,
                    validation_state=target_record.validation_state,
                    lifecycle_state=target_cap.lifecycle_state,
                )
            executor = self._engine._build_executor(
                target_cap,
                proof_sink=getattr(self._fabric, "update_generated_proof", None),
            )
            try:
                await self._fabric.activate_revalidated(
                    generated,
                    executor,
                    owner=(
                        generated.project_scope
                        if target_scope is AffordanceScope.PROJECT
                        else generated.user_scope
                        if target_scope is AffordanceScope.USER
                        else generated.task_scope
                    )
                    or request.task_id
                    or "",
                )
            except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                target_cap.lifecycle_state = original_state
                return _result(request, ok=False, error=f"revalidation persistence failed: {exc}")
            return _result(
                request,
                output=json.dumps(
                    {
                        "capability_id": target_cap.id,
                        "status": "revalidated",
                        "revision": target_cap.revision,
                        "family_id": target_cap.family_id,
                    }
                ),
            )
        name = str(args.get("name") or "").strip()
        if not _NAME.fullmatch(name):
            return _result(request, ok=False, error="invalid generated capability name")

        if operation == "migrate_contract":
            compatibility = str(args.get("compatibility") or "backward_compatible")
            if compatibility not in {"backward_compatible", "breaking"}:
                return _result(
                    request,
                    ok=False,
                    error="compatibility must be backward_compatible or breaking",
                )
            if compatibility == "breaking" and (
                not bool(args.get("operator_confirmation"))
                or request.origin is CapabilityRequestOrigin.MODEL
            ):
                return _result(
                    request,
                    ok=False,
                    error=(
                        "breaking contract migration requires explicit operator confirmation "
                        "from a trusted caller"
                    ),
                )
        else:
            compatibility = "backward_compatible"
        requested_cases = [dict(case) for case in args["validation_cases"]]
        if operation == "repair":
            assert target_cap is not None  # guarded by the predecessor resolution above
            inherited_cases = [
                *(target_cap.validation_cases or []),
                *[
                    case
                    for case in (target_cap.validation.get("regression_cases") or [])
                    if not case.get("resolved_by_revision")
                ],
                *[
                    case
                    for case in (target_cap.validation.get("live_failure_cases") or [])
                    if not case.get("resolved_by_revision")
                ],
            ]
            validation_cases = _merge_validation_cases(inherited_cases, requested_cases)
        elif operation == "migrate_contract" and compatibility == "backward_compatible":
            predecessor_cases = list(target_cap.validation_cases or []) if target_cap else []
            validation_cases = _merge_validation_cases(predecessor_cases, requested_cases)
        else:
            validation_cases = _merge_validation_cases(requested_cases)
        input_schema_arg = args.get("input_schema")
        input_schema = (
            dict(input_schema_arg)
            if input_schema_arg is not None
            else (
                dict(target_cap.input_schema)
                if operation in {"repair", "migrate_contract"} and target_cap is not None
                else infer_input_schema(validation_cases)
            )
        )
        output_schema_arg = args.get("output_schema")
        if operation == "repair" and target_cap is not None:
            if input_schema_arg is not None and input_schema != dict(target_cap.input_schema):
                return _result(
                    request,
                    ok=False,
                    error="repair cannot change the input contract; use migrate_contract",
                )
            if output_schema_arg is not None and dict(output_schema_arg) != dict(
                target_cap.output_schema or {}
            ):
                return _result(
                    request,
                    ok=False,
                    error="repair cannot change the output contract; use migrate_contract",
                )
        if operation == "migrate_contract" and target_cap is not None:
            if input_schema == dict(target_cap.input_schema) and dict(
                output_schema_arg or {}
            ) == dict(target_cap.output_schema or {}):
                return _result(
                    request,
                    ok=False,
                    error="migrate_contract requires an explicit contract change",
                )
        output_schema = (
            dict(output_schema_arg)
            if output_schema_arg is not None
            else (
                dict(target_cap.output_schema or {}) or None
                if operation in {"repair", "migrate_contract"} and target_cap is not None
                else None
            )
        )
        requested_dependencies = tuple(
            DependencyRequirement(
                name=str(dependency["name"]),
                manager=str(dependency.get("manager") or "python"),
                version=dependency.get("version"),
                reason=str(dependency.get("reason") or ""),
                required_for=dependency.get("required_for"),
            )
            for dependency in args.get("required_dependencies") or ()
        )
        required_dependencies = _merge_dependency_requirements(
            target_cap.required_dependencies
            if operation in {"repair", "migrate_contract"} and target_cap
            else (),
            requested_dependencies,
        )
        evidence_dependencies = tuple(
            EvidenceDependency.from_record(dict(dependency))
            for dependency in args.get("evidence_dependencies") or ()
        )
        requested_capabilities = tuple(
            sorted(
                {
                    str(capability).strip()
                    for capability in args.get("required_capabilities") or ()
                    if str(capability).strip()
                }
            )
        )
        required_capabilities = tuple(
            sorted(
                set(
                    target_cap.required_capabilities
                    if operation in {"repair", "migrate_contract"} and target_cap
                    else ()
                )
                | set(requested_capabilities)
            )
        )

        target_id = (
            str(args.get("capability_id") or "")
            if operation in {"repair", "migrate_contract"}
            else ""
        )
        cap = self._engine.synthesize(
            name=name,
            description=str(args.get("description") or ""),
            code=str(args.get("code") or ""),
            runtime=str(args.get("runtime") or "python"),
            input_schema=input_schema,
            output_schema=output_schema,
            effects=set(
                args.get("effects")
                or (
                    target_cap.effects
                    if operation in {"repair", "migrate_contract"} and target_cap is not None
                    else {EffectClass.READ_LOCAL.value}
                )
            ),
            task_id=request.task_id,
            provenance={
                "origin": "model_synthesis",
                "task_id": request.task_id,
                "request_call_id": request.call_id,
                "compatibility": compatibility,
                **(
                    dict(args.get("provenance") or {})
                    if isinstance(args.get("provenance"), Mapping)
                    else {}
                ),
            },
            validation_cases=validation_cases,
            required_dependencies=required_dependencies,
            required_capabilities=required_capabilities,
            evidence_dependencies=(
                target_cap.evidence_dependencies + evidence_dependencies
                if operation in {"repair", "migrate_contract"} and target_cap is not None
                else evidence_dependencies
            ),
            family_id=(
                target_cap.family_id
                if operation in {"repair", "migrate_contract"} and target_cap is not None
                else None
            ),
            revision=(
                target_cap.revision + 1
                if operation in {"repair", "migrate_contract"} and target_cap is not None
                else 1
            ),
            parent_revision=(
                target_cap.revision
                if operation in {"repair", "migrate_contract"} and target_cap is not None
                else None
            ),
            active_revision=(
                target_cap.revision + 1
                if operation in {"repair", "migrate_contract"} and target_cap is not None
                else None
            ),
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
        cap.validation["compatibility"] = compatibility
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
