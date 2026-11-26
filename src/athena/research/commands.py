"""Typed research domain commands and results (review item 31).

Establishes the domain endpoint shape:

    ResearchCapability : CapabilityRequest -> ResearchCommand
    ResearchService    : ResearchCommand   -> ResearchResult
    ResearchCapability : ResearchResult    -> CapabilityResult

The service's domain surface accepts ``ResearchCommand`` and returns
``ResearchResult``; capability request/response translation lives in the
adapter, not in the domain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = ["ResearchCommand", "ResearchResult"]


@dataclass(frozen=True)
class ResearchCommand:
    """One bounded, model-unreachable research operation request."""

    operation: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    project_id: str | None = None
    session_id: str | None = None
    # Call identity for provenance, not for authority decisions.
    call_id: str | None = None


@dataclass(frozen=True)
class ResearchResult:
    """Domain outcome of one research command."""

    ok: bool
    operation: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def success(cls, operation: str, payload: Mapping[str, Any]) -> "ResearchResult":
        return cls(ok=True, operation=operation, payload=dict(payload))

    @classmethod
    def failure(cls, operation: str, error: str) -> "ResearchResult":
        return cls(ok=False, operation=operation, error=error)
