"""Bounded recovery decision projection for the kernel reasoning loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class AdaptiveDecision:
    """One admissible recovery class, never an execution authority."""

    kind: str
    action: str
    reason: str
    remaining_attempts: int = 0
    evidence: Mapping[str, Any] | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "action": self.action,
            "reason": self.reason,
            "remaining_attempts": self.remaining_attempts,
            "evidence": dict(self.evidence or {}),
        }


def project_adaptive_decision(state: Any) -> dict[str, Any] | None:
    """Project the next admissible recovery class from durable run state."""
    records = list(getattr(state, "generated_recovery_records", ()) or ())
    if getattr(state, "generated_recovery_pending", False) and records:
        record = records[-1]
        return AdaptiveDecision(
            kind="implementation_repair",
            action="synthesis.repair",
            reason=str(record.get("failure_class") or "implementation_failure"),
            remaining_attempts=max(
                int(getattr(state, "generated_recovery_limit", 0))
                - int(getattr(state, "generated_recovery_attempts", 0)),
                0,
            ),
            evidence=record,
        ).to_record()
    records = list(getattr(state, "speculative_failure_records", ()) or ())
    if getattr(state, "speculative_recovery_pending", False) and records:
        record = records[-1]
        return AdaptiveDecision(
            kind="strategy_change",
            action="fusion.run_or_compare",
            reason="previous speculative candidate failed verification",
            remaining_attempts=max(
                int(getattr(state, "speculative_recovery_limit", 0))
                - int(getattr(state, "speculative_recovery_attempts", 0)),
                0,
            ),
            evidence=record,
        ).to_record()
    return {
        "kind": "ordinary_reasoning",
        "action": "continue_primary_loop",
        "reason": "no typed recovery transition is pending",
        "remaining_attempts": 0,
        "evidence": {},
    }


__all__ = ["AdaptiveDecision", "project_adaptive_decision"]
