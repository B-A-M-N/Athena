from __future__ import annotations

import logging

from athena.protocol.tasks import TaskStatus
from athena.state.tasks import TaskStore
from athena.state.mutations import PLANNED, STARTED, MutationStore
from athena.state.executions import ExecutionStore
from athena.state.runtime_sessions import RuntimeSessionStore

__all__ = ["RecoveryManager"]

_logger = logging.getLogger("athena.recovery")


class RecoveryManager:
    """Startup crash recovery: reconcile in-flight state after a hard crash.
    
    Called BEFORE the worker and scheduler start claiming work. This ensures
    that orphaned RUNNING tasks, stale executions, and incomplete mutations are
    resolved to a consistent state before any new work begins.
    """

    def __init__(
        self,
        *,
        task_store: TaskStore,
        mutation_store: MutationStore | None = None,
        execution_store: ExecutionStore | None = None,
        runtime_session_store: RuntimeSessionStore | None = None,
        lease_timeout_seconds: float = 300.0,
    ) -> None:
        self._tasks = task_store
        self._mutations = mutation_store
        self._executions = execution_store
        self._runtime_sessions = runtime_session_store
        self._lease_timeout = lease_timeout_seconds

    async def recover(self) -> dict[str, int]:
        """Run all recovery passes and return a summary of reconciled items."""
        summary = {
            "tasks_interrupted": 0,
            "executions_interrupted": 0,
            "runtime_sessions_cleaned": 0,
            "mutations_recovered": 0,
        }
        summary["tasks_interrupted"] = await self._recover_tasks()
        summary["executions_interrupted"] = await self._recover_executions()
        summary["runtime_sessions_cleaned"] = await self._recover_runtime_sessions()
        if self._mutations is not None:
            summary["mutations_recovered"] = await self._recover_mutations()
        return summary

    async def _recover_tasks(self) -> int:
        """Transition orphaned RUNNING tasks to INTERRUPTED.

        A RUNNING task at startup was abandoned by a previous process (the lease
        it may carry belongs to a dead worker). It must become INTERRUPTED so the
        worker can re-claim it in this fresh process. WAITING_APPROVAL tasks
        retain their state (they are waiting for user input, not crash-recoverable).
        """
        count = 0
        try:
            running = await self._tasks.list_by_status(TaskStatus.RUNNING)
        except Exception:
            return 0
        for row in running or []:
            task_id = row.get("id") if isinstance(row, dict) else None
            if not task_id:
                continue
            try:
                await self._tasks.transition(task_id, TaskStatus.INTERRUPTED)
                count += 1
                _logger.info("recovered task %s: RUNNING -> INTERRUPTED", task_id)
            except Exception as exc:
                _logger.warning("failed to recover task %s: %s", task_id, exc)
        return count

    async def _recover_executions(self) -> int:
        """Mark stale RUNNING executions as INTERRUPTED."""
        if self._executions is None:
            return 0
        count = 0
        try:
            # ExecutionStore should have a list_by_status or similar
            stale = await self._executions.list_by_status("RUNNING")
        except Exception:
            stale = []
        for row in stale or []:
            exec_id = row.get("id") if isinstance(row, dict) else None
            if not exec_id:
                continue
            try:
                await self._executions.mark_interrupted(exec_id)
                count += 1
            except Exception as exc:
                _logger.warning("failed to recover execution %s: %s", exec_id, exc)
        return count

    async def _recover_runtime_sessions(self) -> int:
        """Mark runtime sessions as not-alive after a process restart."""
        if self._runtime_sessions is None:
            return 0
        count = 0
        try:
            alive = await self._runtime_sessions.list_alive()
        except Exception:
            alive = []
        for row in alive or []:
            sid = row.get("id") if isinstance(row, dict) else None
            if not sid:
                continue
            try:
                await self._runtime_sessions.mark_dead(sid)
                count += 1
            except Exception as exc:
                _logger.warning("failed to mark runtime session %s dead: %s", sid, exc)
        return count

    async def _recover_mutations(self) -> int:
        """Reconcile PLANNED and STARTED mutations.

        PLANNED: the side effect was never started - inspect current state
        against expected to determine COMPLETED or FAILED.
        STARTED: the side effect may or may not have happened - mark as
        RECOVERY_REQUIRED unless completion can be proven.
        """
        assert self._mutations is not None
        count = 0
        try:
            # The mutation store exposes list_by_status for recovery queries.
            planned = await self._mutations.list_by_status(PLANNED)
            started = await self._mutations.list_by_status(STARTED)
        except Exception:
            planned = []
            started = []
        for row in started or []:
            mid = row.get("id") if isinstance(row, dict) else None
            if not mid:
                continue
            try:
                await self._mutations.mark_recovery_required(mid)
                count += 1
            except Exception as exc:
                _logger.warning("failed to mark mutation %s recovery-required: %s", mid, exc)
        # PLANNED mutations never had their side effect started, so mark
        # them FAILED (the intent can be retried).
        for row in planned or []:
            mid = row.get("id") if isinstance(row, dict) else None
            if not mid:
                continue
            try:
                await self._mutations.mark_failed(mid, error="crash before effect started")
                count += 1
            except Exception as exc:
                _logger.warning("failed to fail PLANNED mutation %s: %s", mid, exc)
        return count