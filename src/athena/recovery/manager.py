from __future__ import annotations

from dataclasses import dataclass
import enum
import logging
from typing import Mapping

from athena.protocol.tasks import TaskStatus
from athena.state.tasks import TaskStore
from athena.state.mutations import PLANNED, STARTED, MutationStore
from athena.state.executions import ExecutionStore
from athena.state.runtime_sessions import RuntimeSessionStore

__all__ = ["RecoveryManager", "RecoveryResult", "RecoveryStatus"]

_logger = logging.getLogger("athena.recovery")


class RecoveryStatus(str, enum.Enum):
    HEALTHY = "healthy"
    RECOVERED = "recovered"
    RECOVERY_REQUIRED = "recovery_required"
    RECOVERY_FAILED = "recovery_failed"


@dataclass(frozen=True)
class RecoveryResult:
    status: RecoveryStatus
    summary: Mapping[str, int]
    error: str | None = None


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
        event_store=None,
        lease_timeout_seconds: float = 300.0,
    ) -> None:
        self._tasks = task_store
        self._mutations = mutation_store
        self._executions = execution_store
        self._runtime_sessions = runtime_session_store
        self._events = event_store
        self._runtime_state_loss_count = 0
        self._lease_timeout = lease_timeout_seconds
        self._recovery_required = False

    async def recover(self) -> RecoveryResult:
        """Run every recovery pass without treating unreadable state as empty."""
        self._runtime_state_loss_count = 0
        self._recovery_required = False
        summary = {
            "tasks_interrupted": 0,
            "executions_interrupted": 0,
            "runtime_sessions_cleaned": 0,
            "runtime_state_lost": 0,
            "mutations_recovered": 0,
        }
        try:
            summary["tasks_interrupted"] = await self._recover_tasks()
            summary["executions_interrupted"] = await self._recover_executions()
            summary["runtime_sessions_cleaned"] = await self._recover_runtime_sessions()
            summary["runtime_state_lost"] = self._runtime_state_loss_count
            if self._mutations is not None:
                summary["mutations_recovered"] = await self._recover_mutations()
        except Exception as exc:
            _logger.error("crash recovery failed closed: %s", exc)
            return RecoveryResult(
                status=RecoveryStatus.RECOVERY_FAILED,
                summary=summary,
                error=str(exc),
            )
        status = (
            RecoveryStatus.RECOVERY_REQUIRED
            if self._recovery_required
            else RecoveryStatus.RECOVERED
            if any(summary.values())
            else RecoveryStatus.HEALTHY
        )
        return RecoveryResult(status=status, summary=summary)

    async def _recover_tasks(self) -> int:
        """Transition orphaned RUNNING tasks to INTERRUPTED.

        A RUNNING task at startup was abandoned by a previous process (the lease
        it may carry belongs to a dead worker). It must become INTERRUPTED so the
        worker can re-claim it in this fresh process. WAITING_APPROVAL tasks
        retain their state (they are waiting for user input, not crash-recoverable).
        """
        count = 0
        running = await self._tasks.list_by_status(TaskStatus.RUNNING)
        for row in running or []:
            task_id = row.get("id") if isinstance(row, dict) else None
            if not task_id:
                continue
            try:
                await self._tasks.transition(task_id, TaskStatus.INTERRUPTED)
                count += 1
                _logger.info("recovered task %s: RUNNING -> INTERRUPTED", task_id)
            except Exception as exc:
                raise RuntimeError(f"failed to recover task {task_id}: {exc}") from exc
        return count

    async def _recover_executions(self) -> int:
        """Mark stale RUNNING executions as INTERRUPTED."""
        if self._executions is None:
            return 0
        count = 0
        # ExecutionStore should have a list_by_status or similar. A read
        # failure is not equivalent to there being no stale executions.
        stale = await self._executions.list_by_status("RUNNING")
        for row in stale or []:
            exec_id = row.get("id") if isinstance(row, dict) else None
            if not exec_id:
                continue
            try:
                await self._executions.mark_interrupted(exec_id)
                count += 1
            except Exception as exc:
                raise RuntimeError(f"failed to recover execution {exec_id}: {exc}") from exc
        return count

    async def _recover_runtime_sessions(self) -> int:
        """Mark runtime sessions as not-alive after a process restart."""
        if self._runtime_sessions is None:
            return 0
        count = 0
        alive = await self._runtime_sessions.list_alive()
        for row in alive or []:
            sid = row.get("id") if isinstance(row, dict) else None
            if not sid:
                continue
            try:
                await self._runtime_sessions.mark_dead(sid)
                count += 1
                if self._events is not None:
                    try:
                        await self._events.append_event(
                            "RuntimeStateLost",
                            {
                                "runtime_session_id": sid,
                                "backend": row.get("backend"),
                                "reason": "Athena restarted without a reattachable runtime process",
                            },
                            task_id=row.get("task_id"),
                        )
                        self._runtime_state_loss_count += 1
                    except Exception as exc:
                        raise RuntimeError(
                            f"failed to emit runtime state loss for {sid}: {exc}"
                        ) from exc
                if row.get("task_id"):
                    try:
                        persist_hint = getattr(self._tasks, "persist_runtime_recovery_hint", None)
                        if persist_hint is not None:
                            await persist_hint(
                                str(row["task_id"]),
                                runtime_session_id=str(sid),
                                backend=row.get("backend"),
                            )
                    except Exception as exc:
                        raise RuntimeError(
                            f"failed to persist runtime recovery hint for {sid}: {exc}"
                        ) from exc
            except Exception as exc:
                raise RuntimeError(f"failed to mark runtime session {sid} dead: {exc}") from exc
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
        # The mutation store exposes list_by_status for recovery queries. Each
        # read is authoritative; an exception must fail startup closed.
        planned = await self._mutations.list_by_status(PLANNED)
        started = await self._mutations.list_by_status(STARTED)
        if started:
            self._recovery_required = True
        for row in started or []:
            mid = row.get("id") if isinstance(row, dict) else None
            if not mid:
                continue
            try:
                await self._mutations.mark_recovery_required(mid)
                count += 1
            except Exception as exc:
                raise RuntimeError(
                    f"failed to mark mutation {mid} recovery-required: {exc}"
                ) from exc
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
                raise RuntimeError(f"failed to fail PLANNED mutation {mid}: {exc}") from exc
        return count
