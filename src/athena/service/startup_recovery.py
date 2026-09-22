"""Durable startup-recovery mechanics beneath ``ServiceLifecycle``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from athena.recovery.manager import RecoveryManager


@dataclass(frozen=True)
class StartupRecoveryPorts:
    """Explicit durable/execution resources required for startup recovery."""

    events: Any
    tasks: Any
    mutations: Any
    execution_store: Any
    runtime_sessions: Any
    execution: Any
    task_manager: Any
    pending_finalization_store: Any


@dataclass(frozen=True)
class StartupRecoveryResult:
    """Recovery evidence projected back to the lifecycle owner."""

    resource_health: dict[str, Any]
    pending_recovered: int
    pending_remaining: int
    completion_recovered: int
    recovery_status: str
    recovery_summary: dict[str, int]
    recovery_error: str | None


class StartupRecovery:
    """Reconcile durable resource, task, and execution state on startup."""

    def __init__(self, ports: StartupRecoveryPorts) -> None:
        self._ports = ports

    async def reconcile(self, *, coordinator: Any, finalizer: Any) -> StartupRecoveryResult:
        await finalizer.load_unresolved()
        await finalizer.reconcile_unresolved()
        resource_health = dict(finalizer.health())

        pending_recovered = await self._ports.task_manager.reconcile_pending_finalizations(
            finalizer
        )
        pending_remaining = await self._ports.pending_finalization_store.list_recoverable()
        completion_recovered = await coordinator.reconcile_startup(self._ports.task_manager)
        recovery = RecoveryManager(
            task_store=self._ports.tasks,
            mutation_store=self._ports.mutations,
            execution_store=self._ports.execution_store,
            runtime_session_store=self._ports.runtime_sessions,
            execution_manager=self._ports.execution,
            event_store=self._ports.events,
        )
        recovery_result = await recovery.recover()
        status = recovery_result.status.value
        if status not in {"healthy", "recovered"}:
            raise RuntimeError(
                "service startup aborted: durable recovery state is "
                f"{status}" + (f": {recovery_result.error}" if recovery_result.error else "")
            )
        return StartupRecoveryResult(
            resource_health=resource_health,
            pending_recovered=pending_recovered,
            pending_remaining=len(pending_remaining),
            completion_recovered=completion_recovered,
            recovery_status=status,
            recovery_summary=dict(recovery_result.summary),
            recovery_error=recovery_result.error,
        )


__all__ = ["StartupRecovery", "StartupRecoveryPorts", "StartupRecoveryResult"]
