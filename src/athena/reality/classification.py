"""Deterministic reality-disposition classification.

This module contains no policy decision and no model input.  It only turns
facts already resolved by the dispatcher into an explainable execution tier.
Keeping the classifier pure makes the safety boundary table-testable and
prevents operation names from becoming an authority mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ExecutionDisposition(str, Enum):
    DIRECT = "direct"
    ISOLATED = "isolated"
    TRANSACTIONAL = "transactional"
    SPECULATIVE = "speculative"


@dataclass(frozen=True)
class RealityClassificationInput:
    """Resolved facts used by :class:`RealityClassifier`."""

    capability_id: str
    operation: str
    effects: frozenset[Any]
    origin: str
    persistent_mutation: bool
    reversible: bool
    target_resources: tuple[str, ...] = ()
    target_breadth: str = "localized"
    command_opacity: bool = False
    process_execution: bool = False
    verification_strength: str = "deferred"
    prior_failure_signal: bool = False
    environment_effects: bool = False
    task_mode: str = "direct"
    checkpoint_available: bool = False
    forced_tier: str | None = None


@dataclass(frozen=True)
class RealityClassification:
    """Explainable result of a deterministic classification."""

    disposition: ExecutionDisposition
    reversibility: str
    blast_radius: str
    opacity: str
    verification: str
    required_verification_floor: str
    workspace_lock_scope: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def to_record(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition.value,
            "reversibility": self.reversibility,
            "blast_radius": self.blast_radius,
            "opacity": self.opacity,
            "verification": self.verification,
            "required_verification_floor": self.required_verification_floor,
            "workspace_lock_scope": list(self.workspace_lock_scope),
            "reasons": list(self.reasons),
        }


class RealityClassifier:
    """Apply conservative, stable rules to resolved execution facts."""

    def classify(self, facts: RealityClassificationInput) -> RealityClassification:
        reasons: list[str] = []
        if facts.forced_tier is not None:
            try:
                disposition = ExecutionDisposition(facts.forced_tier.casefold())
                reasons.append("explicit trusted routing tier")
            except ValueError:
                disposition = self._automatic(facts, reasons)
        else:
            disposition = self._automatic(facts, reasons)

        if facts.prior_failure_signal:
            reasons.append("prior failure signal raises verification scrutiny")
        if facts.environment_effects:
            reasons.append("environment effects require isolated observation")
        floor = (
            "strong"
            if disposition is ExecutionDisposition.SPECULATIVE or facts.target_breadth == "broad"
            else "standard"
        )
        return RealityClassification(
            disposition=disposition,
            reversibility="reversible" if facts.reversible else "uncertain-or-irreversible",
            blast_radius=facts.target_breadth,
            opacity=("opaque" if facts.command_opacity else "transparent"),
            verification=facts.verification_strength,
            required_verification_floor=floor,
            workspace_lock_scope=tuple(sorted(set(facts.target_resources))),
            reasons=tuple(reasons),
        )

    @staticmethod
    def _automatic(
        facts: RealityClassificationInput,
        reasons: list[str],
    ) -> ExecutionDisposition:
        # A process/code executor is not an observation merely because its
        # descriptor omitted WRITE_LOCAL.  Opaque code can write through a
        # child process, an interpreter, or a generated host call, so it must
        # enter the candidate path before the harmless-observation shortcut.
        if facts.command_opacity or facts.process_execution:
            reasons.append("opaque process or generated execution may mutate reality")
            return ExecutionDisposition.SPECULATIVE
        if not facts.persistent_mutation:
            reasons.append("observation has no persistent mutation")
            return ExecutionDisposition.DIRECT
        if facts.environment_effects:
            reasons.append("environment mutation is not workspace-local")
            return ExecutionDisposition.SPECULATIVE
        if facts.task_mode == "speculative":
            reasons.append("task workspace requires a sticky candidate")
            return ExecutionDisposition.SPECULATIVE
        if facts.target_breadth != "localized" or len(facts.target_resources) > 1:
            reasons.append("mutation scope is broader than one localized resource")
            return ExecutionDisposition.SPECULATIVE
        if not facts.reversible:
            reasons.append("mutation is not known to be reversible")
            return ExecutionDisposition.SPECULATIVE
        if facts.checkpoint_available:
            reasons.append("localized reversible mutation with checkpoint support")
            return ExecutionDisposition.TRANSACTIONAL
        reasons.append("localized reversible mutation without checkpoint support")
        return ExecutionDisposition.ISOLATED


__all__ = [
    "ExecutionDisposition",
    "RealityClassification",
    "RealityClassificationInput",
    "RealityClassifier",
]
