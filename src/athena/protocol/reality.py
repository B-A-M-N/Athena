"""Neutral reality-boundary vocabulary and proof-planning contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ExecutionDisposition(str, Enum):
    DIRECT = "direct"
    ISOLATED = "isolated"
    TRANSACTIONAL = "transactional"
    SPECULATIVE = "speculative"


@dataclass(frozen=True)
class CandidateProofPlan:
    """Typed proof-planning result.

    ``criteria`` is executable proof. ``planning_errors`` means proof could
    not be derived; those errors must never be represented as a failing
    shell command.
    """

    criteria: tuple = field(default=())
    required_strength: str = "standard"
    planning_errors: tuple[str, ...] = field(default_factory=tuple)


__all__ = ["CandidateProofPlan", "ExecutionDisposition"]
