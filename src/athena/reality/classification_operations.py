"""Request-to-facts adaptation for the deterministic reality classifier."""

from __future__ import annotations

from typing import Any

from athena.protocol.capabilities import CapabilityDescriptor, CapabilityRequest, EffectClass
from athena.protocol.tasks import MutationMode, WorkspaceSpec
from athena.reality.classification import (
    RealityClassification,
    RealityClassificationInput,
    RealityClassifier,
)
from athena.reality.sensitivity import ISOLATABLE_OPERATIONS, PROCESS_CAPABILITIES

__all__ = ["RealityRequestClassifier"]


class RealityRequestClassifier:
    """Translate a concrete request into facts for the pure classifier."""

    def __init__(self) -> None:
        self._classifier = RealityClassifier()

    def classify(
        self,
        request: CapabilityRequest,
        tier: str | None,
        effects: Any,
        descriptor: CapabilityDescriptor,
        *,
        workspace: WorkspaceSpec | None,
        checkpoint_available: bool,
    ) -> RealityClassification:
        effect_set = set(effects or ())
        args = request.arguments or {}
        operation = str(args.get("operation") or args.get("action") or "").casefold()
        capability_id = str(request.capability_id)
        origin = getattr(descriptor.origin, "value", descriptor.origin)
        resources = tuple(
            str(args[key])
            for key in ("path", "destination", "cwd", "workdir")
            if isinstance(args.get(key), str) and args[key]
        )
        path_count = len(resources)
        opaque = (
            capability_id in PROCESS_CAPABILITIES
            or EffectClass.EXECUTE in effect_set
            or EffectClass.SPAWN_PROCESS in effect_set
            or origin in {"generated", "project", "user"}
        )
        dangerous = bool(
            effect_set
            & {
                EffectClass.DELETE,
                EffectClass.NETWORK_WRITE,
                EffectClass.PRIVILEGED,
                EffectClass.SECRET_READ,
                EffectClass.EXTERNAL_MESSAGE,
                EffectClass.EXTERNAL_PUBLISH,
                EffectClass.COMPUTER_INPUT,
                EffectClass.FINANCIAL,
            }
        )
        reversible = (
            not dangerous
            and operation in ISOLATABLE_OPERATIONS
            and EffectClass.WRITE_LOCAL in effect_set
            and EffectClass.EXTERNAL_MESSAGE not in effect_set
        )
        blast_radius = "localized" if path_count <= 1 else "multi-resource"
        if capability_id in {"workspace", "database"} or path_count > 1:
            blast_radius = "broad"
        facts = RealityClassificationInput(
            capability_id=capability_id,
            operation=operation,
            effects=frozenset(effect_set),
            origin=str(origin),
            persistent_mutation=bool(
                effect_set
                & {
                    EffectClass.WRITE_LOCAL,
                    EffectClass.DELETE,
                    EffectClass.NETWORK_WRITE,
                    EffectClass.EXTERNAL_MESSAGE,
                    EffectClass.EXTERNAL_PUBLISH,
                    EffectClass.COMPUTER_INPUT,
                    EffectClass.FINANCIAL,
                }
            )
            or opaque,
            reversible=reversible and not dangerous,
            target_resources=resources,
            target_breadth=blast_radius,
            command_opacity=opaque,
            process_execution=bool(effect_set & {EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
            verification_strength="deferred-to-completion",
            prior_failure_signal=False,
            environment_effects=bool(
                effect_set
                & {
                    EffectClass.NETWORK_WRITE,
                    EffectClass.EXTERNAL_MESSAGE,
                    EffectClass.EXTERNAL_PUBLISH,
                    EffectClass.COMPUTER_INPUT,
                    EffectClass.FINANCIAL,
                }
            ),
            task_mode=_task_mode(workspace),
            checkpoint_available=checkpoint_available,
            forced_tier=(str(tier) if tier is not None else None),
        )
        return self._classifier.classify(facts)


def _task_mode(workspace: WorkspaceSpec | None) -> str:
    if workspace is None:
        return MutationMode.DIRECT.value
    value = workspace.mutation_mode
    if isinstance(value, MutationMode):
        return value.value
    try:
        return MutationMode(str(value or MutationMode.DIRECT.value)).value
    except ValueError:
        return MutationMode.DIRECT.value
