"""Synthesis admission and operation dispatch mechanism."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from athena.capabilities.synthesis_candidate_builder import build_candidate
from athena.capabilities.synthesis_revalidation import revalidate_target
from athena.capabilities.synthesis_operations import deprecate_capability, inspect_capability
from athena.capabilities.synthesis_validation_admission import validate_and_admit
from athena.protocol.capabilities import CapabilityRequest


async def _resolve_target(
    capability: Any,
    request: CapabilityRequest,
    args: Mapping[str, Any],
    context: Any,
    principal_id: str | None,
):
    from athena.capabilities.synthesis_results import synthesis_result as _result

    operation = str(args.get("operation") or "")
    target_id = str(args.get("capability_id") or "")
    workspace = getattr(context, "workspace", None)
    target_cap = capability._engine.synthetic_for(target_id)
    target_record = None
    if target_cap is None:
        try:
            await capability._fabric.flush()
            for lookup in (
                {"task_id": request.task_id},
                {"project_id": getattr(workspace, "id", None)},
                {"user_id": principal_id},
            ):
                if not any(lookup.values()):
                    continue
                target_record = await capability._fabric.persisted_for(target_id, **lookup)
                if target_record is None:
                    continue
                capability._engine.restore_executor(
                    target_record,
                    proof_sink=getattr(capability._fabric, "update_generated_proof", None),
                    workspace_root=getattr(workspace, "root", None),
                )
                target_cap = capability._engine.synthetic_for(target_id)
                break
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return _result(request, ok=False, error=f"{operation} target restore failed: {exc}")
    target = capability._fabric.provenance(target_id)
    if target is None and target_record is not None:
        target = target_record.to_record()
    if target is None or target_cap is None:
        return _result(
            request,
            ok=False,
            error=f"{operation} target is unknown or has no generated provenance",
        )
    workspace_id = getattr(workspace, "id", None)
    owner_allowed = (
        target.get("task_scope") == request.task_id
        or target.get("project_scope") == workspace_id
        or target.get("user_scope") == principal_id
    )
    if not owner_allowed:
        return _result(request, ok=False, error=f"{operation} target is not visible to this task")
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
    return target_id, target_cap, target_record


async def invoke_synthesis(
    capability: Any,
    request: CapabilityRequest,
    *,
    context: Any = None,
    **kw: Any,
):
    """Route synthesis operations while preserving one capability owner."""
    del kw
    from athena.capabilities.synthesis_results import synthesis_result as _result

    if request.task_id is None:
        return _result(request, ok=False, error="generated capabilities require a task scope")
    args = dict(request.arguments or {})
    operation = str(args.get("operation") or "")
    principal_id = getattr(context, "principal_id", None)
    if operation == "promote_scratch":
        return await capability._op_promote_scratch(request, args, context)
    if operation == "candidates":
        return await capability._op_candidates(request)
    if operation == "inspect":
        return await inspect_capability(capability, request, args)
    if operation == "deprecate":
        return await deprecate_capability(capability, request, args, context, principal_id)
    if operation == "promote":
        return await capability._op_promote(request, args, context)
    target_cap = None
    target_record = None
    if operation in {"repair", "revalidate", "migrate_contract"}:
        resolved = await _resolve_target(capability, request, args, context, principal_id)
        if not isinstance(resolved, tuple):
            return resolved
        _target_id, target_cap, target_record = resolved
        if operation == "revalidate":
            return await revalidate_target(capability, request, context, target_cap, target_record)
    built = build_candidate(capability, request, args, context, operation, target_cap)
    if not isinstance(built, tuple):
        return built
    cap, validation_cases, compatibility = built
    return await validate_and_admit(
        capability,
        request,
        args,
        context,
        cap,
        validation_cases,
        compatibility,
    )
