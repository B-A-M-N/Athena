"""Revalidation lifecycle for previously admitted generated capabilities."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from athena.affordances.models import AffordanceScope
from athena.protocol.capabilities import CapabilityRequest


async def revalidate_target(
    capability: Any,
    request: CapabilityRequest,
    context: Any,
    target_cap: Any,
    target_record: Any,
):
    """Revalidate an unchanged generated capability and reactivate its record.

    The synthesis capability remains the authority for engine and fabric state;
    this module only owns the revalidation workflow between those ports.
    """
    from athena.capabilities.synthesis_results import synthesis_result as _result
    from athena.capabilities.synthesis_support import merge_validation_cases

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
    all_cases = merge_validation_cases(
        target_cap.validation_cases or [],
        target_cap.validation.get("regression_cases") or [],
        target_cap.validation.get("live_failure_cases") or [],
    )
    target_scope = (
        target_record.scope
        if target_record is not None
        else (AffordanceScope.PROJECT if target_cap.task_id is None else AffordanceScope.TASK)
    )
    validation_tier = {
        AffordanceScope.PROJECT: "project",
        AffordanceScope.USER: "user",
        AffordanceScope.TASK: "task",
        AffordanceScope.CANDIDATE: "candidate",
    }[target_scope]
    target_cap = await capability._engine.validate(
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
    evidence = await capability._engine.evidence_status(target_cap, capability._research)
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
    generated = capability._engine._generated_record(
        target_cap,
        scope=target_scope,
        project_scope=(
            target_record.project_scope if target_record else getattr(context.workspace, "id", None)
        ),
        user_scope=(target_record.user_scope if target_record else None),
    )
    if target_record is not None:
        generated = replace(
            generated,
            validation_state=target_record.validation_state,
            lifecycle_state=target_cap.lifecycle_state,
        )
    executor = capability._engine._build_executor(
        target_cap,
        proof_sink=getattr(capability._fabric, "update_generated_proof", None),
    )
    try:
        await capability._fabric.activate_revalidated(
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
