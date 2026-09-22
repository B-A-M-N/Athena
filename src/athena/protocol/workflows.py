"""Neutral workflow execution ports used by orchestration boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class WorkflowRunResult:
    """Neutral result returned by a declared workflow execution port."""

    workflow_id: str
    status: str
    failures: tuple[str, ...] = ()
    suspended: Any = None


class WorkflowRunner(Protocol):
    async def run_declared(self, task: Any, invocation: dict[str, Any]) -> WorkflowRunResult:
        """Run one trusted declared workflow and return its bounded outcome."""


__all__ = ["WorkflowRunResult", "WorkflowRunner"]
