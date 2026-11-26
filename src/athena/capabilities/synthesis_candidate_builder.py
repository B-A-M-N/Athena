"""Build generated capability candidates for synthesis operations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from athena.affordances.models import DependencyRequirement, EvidenceDependency
from athena.protocol.capabilities import CapabilityRequest, CapabilityRequestOrigin, EffectClass
from athena.capabilities.synthesis_descriptor import NAME_PATTERN
from athena.capabilities.synthesis_results import synthesis_result
from athena.capabilities.synthesis_support import (
    merge_dependency_requirements,
    merge_validation_cases,
)


def _synthesis_helpers():
    from athena.capabilities.synthesis import infer_input_schema

    return (
        NAME_PATTERN,
        merge_dependency_requirements,
        merge_validation_cases,
        synthesis_result,
        infer_input_schema,
    )


def _target_id(args: Mapping[str, Any], operation: str) -> str:
    return (
        str(args.get("capability_id") or "") if operation in {"repair", "migrate_contract"} else ""
    )


def build_candidate(
    capability: Any,
    request: CapabilityRequest,
    args: Mapping[str, Any],
    context: Any,
    operation: str,
    target_cap: Any = None,
):
    """Construct a candidate without validating, registering, or promoting it."""
    _NAME, _merge_dependency_requirements, _merge_validation_cases, _result, infer_input_schema = (
        _synthesis_helpers()
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
    requested_cases = [dict(case) for case in args.get("validation_cases") or []]
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
        if input_schema == dict(target_cap.input_schema) and dict(output_schema_arg or {}) == dict(
            target_cap.output_schema or {}
        ):
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
    target_id = _target_id(args, operation)
    cap = capability._engine.synthesize(
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
    return cap, validation_cases, compatibility
