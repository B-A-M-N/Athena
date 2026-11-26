"""Neutral termination decision contract shared by execution domains."""

from __future__ import annotations

from dataclasses import dataclass

from athena.protocol.tasks import TaskStatus


@dataclass(frozen=True)
class TerminationDecision:
    """Outcome of evaluating one model turn."""

    terminal: bool
    reason: str = ""
    status: TaskStatus | None = None
    unresolved: tuple[str, ...] = ()
    summary: str = ""


__all__ = ["TerminationDecision"]
