"""Restart-recovery coordination mechanism for the service façade (P1-10).

Moved verbatim from ``athena.service.service``. This is a subordinate
mechanism, not a second authority: continuation/input stores, the task
store/manager, the kernel, and the recovery-task registry all resolve
through the explicit recovery ports owned by the application service.
Recovery reconstructs only missing durable boundaries after a restart —
it never re-runs model repair and never creates a new task.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from athena.protocol.errors import PersistenceError
from athena.protocol.tasks import TaskStatus
from athena.state.input_requests import InputRequestStore
from athena.state.tasks import TaskStore

__all__ = ["RecoveryCoordinator"]

_logger = logging.getLogger("athena.service")


class RecoveryPorts:
    """Explicit background-recovery ports owned by the application service.

    Recovery may touch only the restart task registry and the application's
    background-failure/tracking seams. All durable inputs arrive as method
    arguments so this coordinator cannot browse or invent lifecycle state.
    """

    _RESOURCE_NAMES = {
        "approval_recovery_tasks": "_approval_recovery_tasks",
        "log_background_failure": "_log_background_failure",
        "track_approval_recovery": "_track_approval_recovery",
    }

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        resource_name = self._RESOURCE_NAMES.get(name)
        if resource_name is None:
            raise AttributeError(f"recovery port is not allowed: {name}")
        return getattr(self._owner, resource_name, None)


class RecoveryCoordinator:
    """Restart recovery: approved continuations, answered inputs, quarantine."""

    def __init__(self, service: Any, *, ports: RecoveryPorts | None = None) -> None:
        self._ports = ports or RecoveryPorts(service)

    async def recover_approved_continuations(
        self,
        *,
        continuations,
        task_store: TaskStore,
        task_manager,
        kernel,
    ) -> None:
        """Resume resolved approval calls after a process restart.

        Approval resolution and the canonical call are durable, but the old
        kernel coroutine is not. This method reconstructs only the missing
        continuation boundary: it never re-runs model repair and never creates
        a new task. A resolved call for a terminal task is left untouched for
        forensic recovery rather than being executed against a completed task.
        """
        try:
            await continuations.release_claims_for_restart()
            task_ids = await continuations.recoverable_task_ids()
        except Exception as exc:
            raise PersistenceError(
                f"approval continuation recovery lookup failed: {exc}",
                cause=exc,
            ) from exc

        for task_id in task_ids:
            try:
                row = await task_store.get(task_id)
            except Exception as exc:
                raise PersistenceError(
                    f"approval continuation task lookup failed for {task_id}: {exc}",
                    cause=exc,
                ) from exc
            if row is None:
                _logger.error("approval continuation %s references missing task", task_id)
                continue

            try:
                status = TaskStatus(row["status"])
            except (KeyError, TypeError, ValueError) as exc:
                raise PersistenceError(
                    f"approval continuation task {task_id} has invalid status",
                    cause=exc,
                ) from exc

            # RecoveryManager converts orphaned RUNNING tasks to INTERRUPTED.
            # WAITING_APPROVAL is the normal hard-crash state. Both are safe to
            # move back to RUNNING for this exact durable continuation.
            if status in (TaskStatus.WAITING_APPROVAL, TaskStatus.INTERRUPTED):
                await task_manager.transition(
                    task_id, TaskStatus.RUNNING, reason="resume approved continuation"
                )
            elif status is not TaskStatus.RUNNING:
                _logger.error(
                    "not resuming approved continuation for task %s in status %s",
                    task_id,
                    status.value,
                )
                continue

            recovery = asyncio.create_task(kernel.run_task(task_id))
            self._ports.approval_recovery_tasks.add(recovery)
            recovery.add_done_callback(self._ports.track_approval_recovery(task_id, recovery))

    def track_approval_recovery(self, task_id: str, recovery: asyncio.Task):
        def _done(task: asyncio.Task) -> None:
            self._ports.approval_recovery_tasks.discard(task)
            self._ports.log_background_failure(f"approval recovery {task_id}")(task)

        return _done

    async def recover_answered_input_requests(
        self,
        *,
        input_requests: InputRequestStore,
        task_store: TaskStore,
        task_manager,
        kernel,
    ) -> None:
        """Resume WAITING_INPUT tasks whose answer arrived while the process was down.

        After ``InputRequestStore.resolve``, the answer is durable but the old
        kernel coroutine is not. Any task in WAITING_INPUT with an
        ANSWERED_PENDING_RESUME input request is a candidate for resume: the
        operator answered while the service was restarting.
        """
        try:
            tasks = await task_store.list_by_status(TaskStatus.WAITING_INPUT)
        except Exception as exc:
            _logger.warning("WAITING_INPUT recovery lookup failed: %s", exc)
            return

        for row in tasks or []:
            task_id = row.get("id") if isinstance(row, dict) else None
            if not task_id:
                continue
            try:
                pending = await input_requests.pending_resumable(task_id)
            except Exception as exc:
                _logger.warning("input-request resumable lookup failed for %s: %s", task_id, exc)
                continue
            if pending is None:
                continue
            try:
                await task_manager.transition(
                    task_id, TaskStatus.RUNNING, reason="resume answered input request"
                )
            except Exception as exc:
                _logger.warning("cannot resume WAITING_INPUT task %s: %s", task_id, exc)
                continue
            recovery = asyncio.create_task(kernel.run_task(task_id))
            self._ports.approval_recovery_tasks.add(recovery)
            recovery.add_done_callback(
                self._ports.log_background_failure(f"input-recovery {task_id}")
            )

    async def quarantine_tasks_for_packs(
        self,
        *,
        task_store: TaskStore,
        task_manager,
        unavailable: set[str],
    ) -> list[str]:
        """Park resumable tasks whose explicit pack dependency is unavailable."""
        if not unavailable:
            return []
        quarantined: list[str] = []
        for status in (TaskStatus.RUNNING, TaskStatus.INTERRUPTED):
            for row in await task_store.list_by_status(status):
                metadata = row.get("metadata") or {}
                required = metadata.get("required_packs") if isinstance(metadata, dict) else ()
                if isinstance(required, str):
                    required = (required,)
                required_ids = {str(item) for item in required or ()}
                missing = sorted(required_ids.intersection(unavailable))
                if not missing:
                    continue
                try:
                    await task_manager.transition(
                        str(row["id"]),
                        TaskStatus.RECOVERY_REQUIRED,
                        reason="required capability pack unavailable: " + ", ".join(missing),
                    )
                except (KeyError, ValueError) as exc:
                    _logger.warning(
                        "could not quarantine task %s for unavailable packs: %s",
                        row.get("id"),
                        exc,
                    )
                    continue
                quarantined.append(str(row["id"]))
        return quarantined
