"""Compatibility re-exports; authority moved to protocol layer."""

from athena.protocol.work_classification import (
    SpeculationDecision,
    SpeculationDepth,
    WorkClass,
    decide_speculation,
)

__all__ = ["SpeculationDecision", "SpeculationDepth", "WorkClass", "decide_speculation"]
