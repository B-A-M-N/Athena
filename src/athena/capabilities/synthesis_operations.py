"""Small lifecycle operation handlers for generated capabilities."""

from __future__ import annotations

import json
from typing import Any


async def promote_scratch_capability(
    owner: Any, request: Any, args: dict[str, Any], context: Any
) -> Any:
    """Validate and admit a task-local scratch program."""
    from athena.capabilities.synthesis_results import synthesis_result as _result

    if owner._scratch is None:
        return _result(request, ok=False, error="scratch promotion is unavailable")
    try:
        program = owner._scratch.get(str(args.get("scratch_id") or ""), task_id=request.task_id)
    except KeyError:
        return _result(request, ok=False, error="unknown scratch_id")
    if not owner._scratch.promotion_ready(program.id):
        return _result(
            request,
            ok=False,
            error="scratch computation needs two distinct successful inputs",
        )
    existing = owner._engine.synthetic_for(program.id)
    if (
        existing is not None
        and existing.task_id == request.task_id
        and owner._fabric.has(program.id, task_id=request.task_id)
    ):
        return _result(
            request,
            output=json.dumps(
                {
                    "capability_id": program.id,
                    "status": "task_reusable",
                    "proof": owner._engine.proof_for(program.id),
                }
            ),
        )
    cases = owner._scratch.validation_cases(program.id)
    cap = owner._engine.synthesize(
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
    cap = await owner._engine.validate(
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
    if not owner._engine.register_ephemeral(owner._fabric, cap):
        return _result(request, ok=False, error="scratch synthesis admission failed")
    return _result(
        request,
        output=json.dumps(
            {
                "capability_id": cap.id,
                "status": "task_reusable",
                "proof": owner._engine.proof_for(cap.id),
            }
        ),
    )


async def list_candidates(owner: Any, request: Any) -> Any:
    """List task-owned generated candidates with their proof metadata."""
    from athena.capabilities.synthesis_results import synthesis_result as _result

    candidates = await owner._fabric.candidates_for(request.task_id)
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
                        dependency.to_record() for dependency in candidate.evidence_dependencies
                    ],
                }
                for candidate in candidates
            ]
        ),
    )


async def promote_capability(owner: Any, request: Any, args: dict[str, Any], context: Any) -> Any:
    """Revalidate and durably promote one owned generated capability."""
    from athena.affordances.models import AffordanceScope
    from athena.capabilities.synthesis_results import synthesis_result as _result

    principal_id = getattr(context, "principal_id", None)
    if context is None:
        return _result(request, ok=False, error="promotion requires workspace context")
    capability_id = str(args.get("capability_id") or "")
    if owner._engine.synthetic_for(capability_id) is None:
        try:
            await owner._fabric.flush()
            candidate = await owner._fabric.persisted_for(capability_id, task_id=request.task_id)
            if candidate is not None and candidate.scope is AffordanceScope.CANDIDATE:
                owner._engine.restore_executor(
                    candidate,
                    proof_sink=getattr(owner._fabric, "update_generated_proof", None),
                    workspace_root=context.workspace.root,
                )
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return _result(request, ok=False, error=f"candidate restore failed: {exc}")
    scope = str(args.get("scope") or "")
    project_id = context.workspace.id if scope == "project" else None
    if scope == "user" and not principal_id:
        return _result(request, ok=False, error="user capability promotion requires principal")
    user_id = principal_id if scope == "user" else ""
    try:
        cap = owner._engine.synthetic_for(capability_id)
        if cap is None:
            return _result(request, ok=False, error="capability is not validated or unknown")
        cap = await owner._engine.validate(
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
        evidence = await owner._engine.evidence_status(cap, owner._research)
        cap.validation["evidence"] = evidence
        if evidence["status"] != "CURRENT":
            return _result(
                request,
                ok=False,
                error="promotion evidence is stale or unavailable",
                output=json.dumps({"capability_id": capability_id, "evidence": evidence}),
            )
        if not cap.validation.get("all_passed"):
            return _result(
                request,
                ok=False,
                error="promotion validation failed",
                output=json.dumps({"validation": cap.validation}),
            )
        promoted = await owner._engine.promote(
            owner._fabric,
            capability_id,
            scope=AffordanceScope(scope),
            project_id=project_id,
            user_id=user_id,
        )
        if promoted:
            await owner._fabric.flush()
    except (TypeError, ValueError) as exc:
        return _result(request, ok=False, error=str(exc))
    except (OSError, RuntimeError) as exc:
        return _result(request, ok=False, error=f"promotion persistence failed: {exc}")
    if not promoted:
        return _result(
            request,
            ok=False,
            error="capability is not validated, unknown, or lacks diverse live promotion proof",
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


async def inspect_capability(owner: Any, request: Any, args: dict[str, Any]) -> Any:
    """Read one owned generated capability without changing its lifecycle."""
    from athena.affordances.models import AffordanceScope
    from athena.capabilities.synthesis_results import synthesis_result as _result

    capability_id = str(args.get("capability_id") or "")
    candidate = await owner._fabric.persisted_for(capability_id, task_id=request.task_id)
    if candidate is None:
        candidate = owner._engine.synthetic_for(capability_id)
    if candidate is None:
        return _result(request, ok=False, error="capability is unknown or not owned")
    if hasattr(candidate, "to_record"):
        value = candidate.to_record()
    else:
        value = owner._engine._generated_record(candidate, scope=AffordanceScope.TASK).to_record()
    return _result(request, output=json.dumps(value))


async def deprecate_capability(
    owner: Any, request: Any, args: dict[str, Any], context: Any, principal_id: str | None
) -> Any:
    """Apply the durable owner-scoped deprecation transition."""
    from athena.capabilities.synthesis_results import synthesis_result as _result

    workspace = getattr(context, "workspace", None)
    try:
        deprecated = await owner._fabric.deprecate(
            str(args.get("capability_id") or ""),
            task_id=request.task_id,
            project_id=getattr(workspace, "id", None),
            user_id=principal_id,
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
        output=json.dumps({"capability_id": args["capability_id"], "status": "deprecated"}),
    )


__all__ = ["deprecate_capability", "inspect_capability"]
