"""Immutable prepared capability call (review item 4).

Resolved exactly once by ``CapabilityDispatcher._prepare_call()``: the
executor, exact effects, and resource locks are computed a single time and
carried on this frozen object through ordering, policy, and invocation.
Batched and direct dispatches observe the same registry/fabric snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    DispatchDirectives,
    EffectClass,
    ResourceClass,
)
from athena.protocol.tasks import WorkspaceSpec


@dataclass(frozen=True)
class PreparedCapabilityCall:
    """One canonical, resolved capability call."""

    request: CapabilityRequest
    workspace: WorkspaceSpec
    executor: Any
    effects: tuple[EffectClass, ...]
    directives: DispatchDirectives | None = None
    resource_keys: tuple[str, ...] = ()
    failure: "PreparationFailure | None" = None
    # Immutable resolution snapshot (item 17)
    canonical_request: CapabilityRequest | None = None
    descriptor: "CapabilityDescriptor | None" = None
    resource_classes: tuple[ResourceClass, ...] = ()
    repair_receipt: Any = None
    registry_generation: int | None = None

    def with_effects(self, effects: tuple[EffectClass, ...]) -> "PreparedCapabilityCall":
        """Return a copy carrying the finally-resolved exact effects."""
        return PreparedCapabilityCall(
            request=self.request,
            workspace=self.workspace,
            executor=self.executor,
            effects=effects,
            directives=self.directives,
            resource_keys=self.resource_keys,
            failure=self.failure,
            canonical_request=self.canonical_request,
            descriptor=self.descriptor,
            resource_classes=self.resource_classes,
            repair_receipt=self.repair_receipt,
            registry_generation=self.registry_generation,
        )

    @property
    def call_id(self) -> str:
        return self.request.call_id

    @property
    def capability_id(self) -> str:
        return self.request.capability_id


@dataclass(frozen=True)
class PreparationFailure:
    """Typed classification of why a prepared call cannot execute.

    Replaces the overloaded ``executor=None`` sentinel so schema failures,
    failed repair, unknown capability, and effect-contract failures are
    distinguishable without parsing error strings.
    """

    code: str  # unknown_capability | repair_invalid | schema_validation | effect_contract
    detail: str = ""
    schema_errors: tuple[str, ...] = ()
    repair_receipt: Any = None
