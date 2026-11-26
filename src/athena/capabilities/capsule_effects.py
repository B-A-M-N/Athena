"""Admission-time effect analysis for portable procedure capsules."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from athena.affordances.models import GeneratedCapability
from athena.protocol.capabilities import CapabilityDescriptor, EffectClass
from athena.workflows.models import Workflow
from athena.workflows.validation import WorkflowValidator


def resolve_capsule_effects(
    capsule: Mapping[str, Any],
    *,
    fabric,
    workspace,
    task_id: str | None,
    principal_id: str | None,
) -> tuple[EffectClass, ...]:
    """Return the fail-closed effect envelope for a capsule's root graph."""
    records = list(capsule.get("workflows") or ())
    if not records:
        raise ValueError("capsule contains no workflows")
    workflows: dict[str, Workflow] = {}
    for record in records:
        workflow = Workflow.from_record(dict(record))
        workflows[workflow.id] = workflow
    root_id = str(capsule.get("root_workflow_id") or "")
    root = workflows.get(root_id)
    if root is None:
        raise ValueError("capsule root workflow is missing")
    exported = {
        str(item.get("id") or ""): item
        for item in (capsule.get("capabilities") or ())
        if isinstance(item, Mapping)
    }

    def resolver(identifier):
        nested = workflows.get(identifier)
        if nested is not None:
            return nested
        item = exported.get(str(identifier))
        generated_record = item.get("generated") if item else None
        if generated_record:
            generated = GeneratedCapability.from_record(generated_record)
            return CapabilityDescriptor(
                id=generated.id,
                description=generated.description,
                input_schema=dict(generated.input_schema),
                effects=frozenset(EffectClass(effect) for effect in generated.declared_effects),
            )
        return fabric.executor_for(
            identifier,
            task_id=task_id,
            project_id=getattr(workspace, "id", None),
            user_id=principal_id,
        ).descriptor

    validation = WorkflowValidator(resolver).validate(root)
    if not validation.ok:
        raise ValueError("capsule effect graph is not admissible: " + "; ".join(validation.errors))
    effects = {EffectClass(effect) for effect in validation.effects}
    # Importing the capsule persists task-local records before replay.
    effects.add(EffectClass.WRITE_LOCAL)
    if EffectClass.EXECUTE in effects or EffectClass.SPAWN_PROCESS in effects:
        # Process steps are not statically provable to be network-free.
        effects.add(EffectClass.NETWORK_WRITE)
    return tuple(sorted(effects, key=lambda effect: effect.value))


__all__ = ["resolve_capsule_effects"]
