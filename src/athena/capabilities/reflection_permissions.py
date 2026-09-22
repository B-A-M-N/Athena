"""Read-only policy and approval projection for capability reflection."""

from __future__ import annotations

from typing import Any

__all__ = ["list_permissions"]


async def list_permissions(
    reflection: Any,
    *,
    capability_id: str,
    task_id: str | None,
    project_id: str | None,
    user_id: str | None,
    context=None,
) -> list[dict]:
    """Project effective permissions without granting or changing authority."""
    descriptors = reflection._fabric.list_descriptors(
        task_id=task_id,
        project_id=project_id,
        user_id=user_id,
    )
    if capability_id:
        descriptors = [item for item in descriptors if item.id == capability_id]

    task_policy = getattr(context, "capability_policy", None)
    task_policy_record = None
    if task_policy is not None:
        task_policy_record = {
            "allow": list(task_policy.allow),
            "ask": list(task_policy.ask),
            "deny": list(task_policy.deny),
            "effects": sorted(
                getattr(effect, "value", str(effect)) for effect in task_policy.effects
            ),
        }

    pending: list[dict] = []
    if reflection._approvals is not None and task_id is not None:
        for record in await reflection._approvals.list_pending(task_id):
            # Approval arguments are intentionally omitted: reflection is
            # model-visible and arguments may contain credentials or data.
            pending.append(
                {
                    "approval_id": record.get("id"),
                    "capability_id": record.get("capability_id"),
                    "status": record.get("status"),
                    "created_at": record.get("created_at"),
                }
            )

    grants: list[dict] = []
    manager = getattr(reflection._policy, "approvals", None)
    if manager is not None:
        for grant in manager.list_active():
            if grant.task_id not in (None, task_id):
                continue
            grants.append(
                {
                    "approval_id": grant.id,
                    "capability_id": grant.capability,
                    "effect": getattr(grant.effect, "value", grant.effect),
                    "scope": getattr(grant.scope, "value", grant.scope),
                    "resource_pattern": grant.resource_pattern,
                    "task_id": grant.task_id,
                    "session_id": grant.session_id,
                    "expires_at": (grant.expires_at.isoformat() if grant.expires_at else None),
                }
            )

    workspace = getattr(context, "workspace", None)
    raw_profile = getattr(reflection._policy, "profile", None)
    profile = getattr(raw_profile, "value", raw_profile)
    permissions = [
        {
            "kind": "permission",
            "capability_id": descriptor.id,
            "declared_effects": sorted(effect.value for effect in descriptor.effects),
            "availability": descriptor.availability.value,
            "task_allowed": not (
                task_policy is not None
                and (
                    descriptor.id in task_policy.deny
                    or (bool(task_policy.allow) and descriptor.id not in task_policy.allow)
                )
            ),
            "task_requires_approval": bool(
                task_policy is not None and descriptor.id in task_policy.ask
            ),
            "task_effect_ceiling": task_policy_record["effects"]
            if task_policy_record is not None
            else [],
        }
        for descriptor in descriptors
    ]
    return [
        {
            "kind": "policy_context",
            "profile": profile,
            "workspace_id": getattr(workspace, "id", None),
            "network_policy": getattr(
                getattr(workspace, "network_policy", None),
                "value",
                getattr(workspace, "network_policy", None),
            ),
            "mutation_mode": getattr(
                getattr(workspace, "mutation_mode", None),
                "value",
                getattr(workspace, "mutation_mode", None),
            ),
            "task_policy": task_policy_record,
            "pending_approvals": pending,
            "active_grants": grants,
        },
        *permissions,
    ]
