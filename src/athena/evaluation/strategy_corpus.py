"""Fixed strategy/escalation cases for pre-model behavior regression tests."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyEscalationCase:
    id: str
    objective: str
    expected_decision: str
    expected_completion_mode: str
    required_escalation: str


def strategy_escalation_corpus() -> tuple[StrategyEscalationCase, ...]:
    return (
        StrategyEscalationCase("response", "explain recursion", "respond", "response_only", "none"),
        StrategyEscalationCase(
            "observe",
            "what changed in this repo?",
            "discover",
            "observable_work_required",
            "workspace_observation",
        ),
        StrategyEscalationCase(
            "mutate", "fix the failing test", "act", "observable_work_required", "verification"
        ),
        StrategyEscalationCase(
            "continue", "continue", "act", "observable_work_required", "prior_context"
        ),
        StrategyEscalationCase(
            "approval", "send this message", "act", "observable_work_required", "operator_approval"
        ),
        StrategyEscalationCase(
            "network",
            "research the latest release",
            "discover",
            "observable_work_required",
            "network_policy",
        ),
        StrategyEscalationCase(
            "recovery",
            "resume after interruption",
            "act",
            "observable_work_required",
            "recovery_receipt",
        ),
        StrategyEscalationCase(
            "ambiguous",
            "do the thing",
            "discover",
            "observable_work_required",
            "clarification_or_safe_probe",
        ),
    )


__all__ = ["StrategyEscalationCase", "strategy_escalation_corpus"]
