"""Admission-time effect analysis for owner-scoped workflow graphs."""

from __future__ import annotations

from typing import Any

from athena.protocol.capabilities import EffectClass
from athena.workflows.validation import WorkflowValidator


async def resolve_workflow_effects(
    arguments,
    *,
    store,
    fabric,
    load_graph,
    workspace,
    task_id: str | None,
    principal_id: str | None,
) -> tuple[EffectClass, ...]:
    """Resolve effects from every branch of an owner-scoped workflow graph."""
    workflow_id = str((arguments or {}).get("workflow_id") or "")
    if not workflow_id:
        raise ValueError("workflow_id is required for workflow effect resolution")
    root = await store.get(
        workflow_id,
        task_id=task_id,
        project_id=getattr(workspace, "id", None),
        user_id=principal_id,
    )
    if root is None:
        raise ValueError(f"workflow not found or not owned: {workflow_id}")
    graph = await load_graph(
        root,
        task_id=task_id,
        project_id=getattr(workspace, "id", None),
        user_id=principal_id,
    )

    def resolver(identifier):
        nested = graph.get(identifier)
        if nested is not None:
            return nested
        return fabric.executor_for(
            identifier,
            task_id=task_id,
            project_id=getattr(workspace, "id", None),
            user_id=principal_id,
        ).descriptor

    validation = WorkflowValidator(resolver).validate(root)
    if not validation.ok:
        raise ValueError("workflow effect graph is not admissible: " + "; ".join(validation.errors))
    effects = {EffectClass(effect) for effect in validation.effects}
    # Process/code steps may mutate the workspace and are not statically
    # provable to be network-free; child dispatch still enforces the policy.
    if EffectClass.EXECUTE in effects or EffectClass.SPAWN_PROCESS in effects:
        effects.update({EffectClass.WRITE_LOCAL, EffectClass.NETWORK_WRITE})
    return tuple(sorted(effects, key=lambda effect: effect.value)) or (EffectClass.READ_LOCAL,)


__all__ = ["resolve_workflow_effects"]
