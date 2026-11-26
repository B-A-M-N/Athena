"""Typed Fusion ports for service-decoupled composition.

Fusion depends on explicit capability ports, not the whole application
facade. The legacy service path is retained for existing construction, but
all newly composed systems should supply :class:`FusionPorts`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

__all__ = ["CandidateVerifierPort", "FusionPorts", "RealityGatePort", "TaskForkPorts"]


class CandidateVerifierPort(Protocol):
    """Protocol for the canonical candidate verifier service."""

    async def verify_candidate(self, *args: Any, **kwargs: Any) -> list[dict]: ...

    async def impact_for(self, root: str, changed_resources: tuple[str, ...]) -> dict: ...


class RealityGatePort(Protocol):
    """Protocol for the execution authority's candidate routing boundary."""

    def active_branch(self, task_id: str) -> Any | None: ...

    async def deactivate_branch(self, task_id: str) -> None: ...


@dataclass
class TaskForkPorts:
    """Explicit dependencies required to fork a task causally."""

    task_store: Any
    task_manager: Any
    event_store: Any
    session_store: Any
    message_store: Any | None = None
    checkpoint_manager: Any | None = None
    principal_id: str | None = None


@dataclass
class FusionPorts:
    """Explicit Fusion runtime dependencies."""

    shadow: Any
    checkpoints: Any
    task_store: Any | None = None
    event_store: Any | None = None
    runtime_session_store: Any | None = None
    context_block_store: Any | None = None
    fabric: Any | None = None
    workflow_store: Any | None = None
    synthesis: Any | None = None
    synthesis_ref: Any | None = None
    dispatcher: Any | None = None
    verification_environment: Any | None = None
    verification_environment_resolver: Callable[[Any], Any] | None = None
    budget_provider: Any | None = None
    world_state_provider: Callable[[str], Any] | None = None
    default_workspace: Any | None = None
    world_state_store: Any | None = None
    world_state_factory: Callable[[str], Any] | None = None
    reality_coordinator: CandidateVerifierPort | None = None
    candidate_verifier: CandidateVerifierPort | None = None
    reality_gate: RealityGatePort | None = None
    fork: TaskForkPorts | None = None
    service: Any | None = None
    compatibility: dict[str, Any] = field(default_factory=dict)
