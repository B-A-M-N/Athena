"""Capability availability passport extraction.

Reflection is advisory. This mechanism composes bounded availability checks;
it does not grant authority or become a second policy engine.
"""

from __future__ import annotations

from athena.protocol.errors import CapabilityUnavailable

__all__ = ["explain_availability"]


async def explain_availability(
    reflection,
    capability_id: str,
    capability_arguments: dict,
    *,
    task_id: str | None,
    project_id: str | None,
    user_id: str | None,
    context=None,
) -> dict:
    """Compute whether a capability can run in this task context."""
    checks: list[dict] = []
    preconditions: list[str] = []
    descriptor = None
    provenance = reflection._fabric.provenance(capability_id)
    try:
        descriptor = reflection._fabric.executor_for(
            capability_id,
            task_id=task_id,
            project_id=project_id,
            user_id=user_id,
        ).descriptor
        checks.append({"kind": "capability", "status": "available"})
    except CapabilityUnavailable as exc:
        lifecycle = str((provenance or {}).get("lifecycle_state") or "")
        status = "stale" if lifecycle in {"STALE", "REVALIDATION_REQUIRED"} else "unavailable"
        checks.append(
            {
                "kind": "capability",
                "status": status,
                "detail": str(exc),
                **({"lifecycle_state": lifecycle} if lifecycle else {}),
            }
        )

    if descriptor is None:
        return {
            "kind": "environment_passport",
            "capability_id": capability_id,
            "status": "BLOCKED",
            "checks": checks,
            "preconditions": preconditions,
            "next_steps": ["inspect the capability surface or build a replacement"],
        }

    workspace = getattr(context, "workspace", None)
    dependency_available, environment_compatible = reflection._fabric.prerequisite_status(
        capability_id,
        task_id=task_id,
        project_id=project_id,
        user_id=user_id,
        workspace=workspace,
    )
    if not dependency_available:
        checks.append(
            {
                "kind": "prerequisites",
                "status": "missing",
                "detail": "required capabilities or dependencies are unavailable",
            }
        )
        preconditions.append("required capability/dependency prerequisites are unavailable")
    elif not environment_compatible:
        checks.append(
            {
                "kind": "environment",
                "status": "incompatible",
                "detail": "the current environment does not match the capability proof",
            }
        )
        preconditions.append("the current environment does not match capability proof")

    if reflection._health is not None:
        health = reflection._health.get(capability_id)
        health_status = str(health.get("status") or "closed")
        checks.append(
            {
                "kind": "health",
                "status": "open"
                if health_status == "open"
                else ("probing" if health_status == "half_open" else "healthy"),
                "consecutive_failures": health.get("consecutive_failures", 0),
                "retry_after_seconds": health.get("retry_after_seconds", 0.0),
            }
        )
        if health_status == "open":
            preconditions.append("capability circuit is open; wait for its cooldown")

    try:
        effects = descriptor.resolve_effects(capability_arguments)
    except (TypeError, ValueError):
        effects = None
    effective_effects = effects or descriptor.effects
    effect_values = {getattr(effect, "value", str(effect)) for effect in effective_effects}

    runtime_name = (
        capability_arguments.get("runtime")
        or capability_arguments.get("language")
        or (provenance or {}).get("runtime")
    )
    if runtime_name and reflection._execution is not None:
        runtime_available = reflection._execution.has_runtime(str(runtime_name))
        checks.append(
            {
                "kind": "runtime",
                "id": str(runtime_name),
                "status": "available" if runtime_available else "missing",
            }
        )
        if not runtime_available:
            preconditions.append(f"runtime {runtime_name!r} is unavailable")

    execution_backend = getattr(workspace, "execution_backend", None)
    if execution_backend and reflection._execution is not None:
        backend_status = getattr(reflection._execution, "backend_status", None)
        statuses = list(backend_status()) if callable(backend_status) else []
        selected = next(
            (item for item in statuses if item.get("id") == execution_backend),
            None,
        )
        if selected is not None:
            backend_available = bool(selected.get("available", False))
            checks.append(
                {
                    "kind": "execution_backend",
                    "id": execution_backend,
                    "status": "available" if backend_available else "missing",
                    "healthy": bool(selected.get("healthy", backend_available)),
                }
            )
            if not backend_available:
                preconditions.append(f"execution backend {execution_backend!r} is unavailable")

    network_policy = getattr(
        getattr(workspace, "network_policy", None),
        "value",
        getattr(workspace, "network_policy", None),
    )
    needs_network = bool({"NETWORK_READ", "NETWORK_WRITE"} & effect_values)
    if needs_network and network_policy == "deny":
        checks.append(
            {"kind": "network", "status": "blocked", "detail": "workspace network policy is deny"}
        )
        preconditions.append("workspace network policy must allow this operation")
    elif needs_network:
        browser_restricted_unavailable = (
            capability_id == "browser" and network_policy == "restricted"
        )
        checks.append(
            {
                "kind": "network",
                "status": (
                    "unavailable"
                    if browser_restricted_unavailable
                    else ("restricted" if network_policy == "restricted" else "available")
                ),
                "policy": network_policy or "unknown",
                **(
                    {
                        "detail": (
                            "browser restricted networking requires an Athena-controlled "
                            "DNS-pinned proxy"
                        )
                    }
                    if browser_restricted_unavailable
                    else {}
                ),
            }
        )
        if browser_restricted_unavailable:
            preconditions.append(
                "browser driver must advertise Athena-controlled DNS-pinned proxy enforcement"
            )

    task_policy = getattr(context, "capability_policy", None)
    policy_status = "allowed"
    if task_policy is not None:
        if capability_id in task_policy.deny or (
            task_policy.allow and capability_id not in task_policy.allow
        ):
            policy_status = "denied"
            preconditions.append("task capability policy denies this capability")
        elif capability_id in task_policy.ask:
            policy_status = "approval_required"
            preconditions.append("operator approval is required")
        ceiling = {getattr(effect, "value", str(effect)) for effect in (task_policy.effects or ())}
        if ceiling and not effect_values.issubset(ceiling):
            policy_status = "denied"
            preconditions.append("capability effects exceed the task ceiling")
    checks.append({"kind": "policy", "status": policy_status, "effects": sorted(effect_values)})

    blocked = any(
        item["status"] in {"missing", "blocked", "denied", "stale", "open"} for item in checks
    )
    approval = any(item["status"] == "approval_required" for item in checks)
    status = "BLOCKED" if blocked else "REQUIRES_APPROVAL" if approval else "AVAILABLE"
    return {
        "kind": "environment_passport",
        "capability_id": capability_id,
        "status": status,
        "checks": checks,
        "preconditions": preconditions,
        "next_steps": ["resolve the listed preconditions before invoking"] if preconditions else [],
    }
