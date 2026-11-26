"""Explicit Fusion composition beneath :class:`AthenaService`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from athena.fusion.ports import FusionPorts, TaskForkPorts


@dataclass(frozen=True)
class FusionComposition:
    """Named service resources required to construct the typed Fusion ports."""

    shadow: Any
    checkpoints: Any
    task_store: Any
    event_store: Any
    runtime_session_store: Any
    context_block_store: Any
    fabric: Any
    workflow_store: Any
    synthesis: Any
    dispatcher: Any
    verification_environment_resolver: Any
    budget_provider: Any
    default_workspace: Any
    world_state_store: Any
    world_state_provider: Any
    reality_coordinator: Any
    reality_gate: Any
    task_manager: Any
    session_store: Any
    message_store: Any
    principal_id: str | None

    def build(self) -> Any:
        """Build the single typed Fusion orchestrator for these resources."""
        from athena.fusion.orchestrator import FusionOrchestrator

        return FusionOrchestrator(
            typed_ports=FusionPorts(
                shadow=self.shadow,
                checkpoints=self.checkpoints,
                task_store=self.task_store,
                event_store=self.event_store,
                runtime_session_store=self.runtime_session_store,
                context_block_store=self.context_block_store,
                fabric=self.fabric,
                workflow_store=self.workflow_store,
                synthesis=self.synthesis,
                dispatcher=self.dispatcher,
                verification_environment_resolver=self.verification_environment_resolver,
                budget_provider=self.budget_provider,
                default_workspace=self.default_workspace,
                world_state_store=self.world_state_store,
                world_state_provider=self.world_state_provider,
                reality_coordinator=self.reality_coordinator,
                reality_gate=self.reality_gate,
                fork=TaskForkPorts(
                    task_store=self.task_store,
                    task_manager=self.task_manager,
                    event_store=self.event_store,
                    session_store=self.session_store,
                    message_store=self.message_store,
                    principal_id=self.principal_id,
                ),
            )
        )


__all__ = ["FusionComposition"]
