"""Deterministic affordance guidance for the single Athena model loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class StrategyGuidance:
    """A small, model-visible hint—not a second planner or execution path."""

    route: str
    rationale: str
    candidates: tuple[str, ...] = ()
    missing_affordance: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "route": self.route,
            "rationale": self.rationale,
            "candidates": self.candidates,
            "missing_affordance": self.missing_affordance,
        }


def select_strategy(objective: str, capability_ids: Iterable[str]) -> StrategyGuidance:
    """Select bounded affordance guidance from objective words and availability.

    The model still chooses the actual calls. This function only makes the
    existing architecture explicit and observable, which prevents strategy
    discovery from depending on an accidental prompt formulation.
    """
    text = str(objective or "").casefold()
    available = {str(value) for value in capability_ids}

    # An empty inventory is different from a known missing affordance.  Small
    # compiler/unit fixtures and restricted deployments may intentionally
    # expose no capabilities; do not turn that absence into a model-facing
    # claim that a preferred capability should be built.
    if not available:
        return StrategyGuidance(
            route="direct",
            rationale="No capability inventory is available; follow the task with the currently exposed interface.",
        )

    preferred: tuple[str, ...]
    if any(word in text for word in ("experiment", "shadow", "speculative", "fork")):
        preferred = ("fusion", "workflow", "fs", "execute")
        route = "fusion"
        rationale = (
            "The objective describes bounded speculative work; prove it in a shadow before commit."
        )
    elif any(
        word in text for word in ("research", "compare", "sources", "evidence", "investigate")
    ):
        preferred = ("research", "workflow", "artifacts")
        route = "evidence_acquisition"
        rationale = (
            "The objective needs sourced evidence, bounded acquisition, and explicit gap handling."
        )
    elif any(
        word in text
        for word in ("build a tool", "create a tool", "automate", "generate a capability")
    ):
        preferred = ("synthesis", "scratch", "workflow")
        route = "synthesize"
        rationale = (
            "The objective suggests a reusable affordance; validate task-locally before promotion."
        )
    elif any(word in text for word in ("workflow", "pipeline", "release", "deploy")):
        preferred = ("workflow", "execute", "fs")
        route = "compose"
        rationale = "The objective spans ordered steps; prefer a bounded workflow when one exists."
    else:
        preferred = ("capabilities", "execute", "fs", "workflow", "scratch", "synthesis")
        route = "direct"
        rationale = "Start with the smallest existing capability; compose or build only if the affordance is insufficient."

    candidates = tuple(capability for capability in preferred if capability in available)
    # The first candidate names the selected route's primary affordance.  A
    # convenient fallback must not make a materially different route look
    # equivalent to the requested one.
    missing = preferred[0] if preferred and preferred[0] not in available else None
    if missing is not None:
        return StrategyGuidance(
            route="affordance_gap",
            rationale=f"Preferred route {missing!r} is not currently available; inspect or build a bounded replacement.",
            candidates=(),
            missing_affordance=missing,
        )
    return StrategyGuidance(route, rationale, candidates)


__all__ = ["StrategyGuidance", "select_strategy"]
