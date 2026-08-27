from __future__ import annotations

import asyncio
import logging
from typing import Any

from athena.protocol.tasks import TaskStatus

__all__ = [
    "CancellationManager",
]

_logger = logging.getLogger("athena.cancellation")


class CancellationManager:
    """Hierarchical, idempotent cancellation (BUILDSPEC §20, BHV-022/023).

    Each task may have an ``asyncio.Event`` cancellation token checked by the
    kernel at turn boundaries. Cancelling a task sets its own token and
    propagates to every descendant so child work is interrupted (INV-001: work
    still ends through the kernel; this manager only signals and transitions).
    """

    def __init__(
        self,
        *,
        task_manager: Any = None,
        execution_manager: Any = None,
        task_store: Any = None,
    ) -> None:
        self._tasks = task_manager
        self._exec = execution_manager
        self._store = task_store if task_store is not None else getattr(task_manager, "_store", None)
        self._tokens: dict[str, asyncio.Event] = {}
        self._reasons: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    def reset(self, task_id: str) -> None:
        self._tokens.pop(task_id, None)
        self._reasons.pop(task_id, None)

    def register(self, task_id: str) -> asyncio.Event:
        ev = self._tokens.get(task_id)
        if ev is None:
            ev = asyncio.Event()
            self._tokens[task_id] = ev
        return ev

    def token(self, task_id: str) -> asyncio.Event:
        return self._tokens.get(task_id, asyncio.Event())

    def reason(self, task_id: str) -> str:
        return self._reasons.get(task_id, "")

    def is_cancelled(self, task_id: str) -> bool:
        ev = self._tokens.get(task_id)
        return bool(ev and ev.is_set())

    # ------------------------------------------------------------------ #
    async def cancel(self, task_id: str, reason: str = "cancelled by user") -> TaskStatus:
        await self._cancel_impl(task_id, reason, root_id=task_id)
        return TaskStatus.CANCELLED

    def set_token(self, task_id: str, reason: str = "cancelled") -> None:
        ev = self.register(task_id)
        ev.set()
        self._reasons[task_id] = reason

    async def interrupt(self, task_id: str, reason: str = "externally interrupted") -> TaskStatus:
        """Recoverable interruption (BHV-023); does not set the terminal token.

        The task and every descendant are parked as ``INTERRUPTED`` and MAY be
        resumed later by re-acquiring them (BUILDSPEC §87-89).
        """
        self._reasons[task_id] = reason
        for desc in await self._descendants_of(task_id):
            self.set_token(desc, reason)
            await self._transition_status(desc, TaskStatus.INTERRUPTED)
        await self._transition_status(task_id, TaskStatus.INTERRUPTED)
        return TaskStatus.INTERRUPTED

    async def cancel_tree(self, task_id: str, reason: str = "cancelled") -> None:
        await self.cancel(task_id, reason)

    # ------------------------------------------------------------------ #
    async def _cancel_impl(self, task_id: str, reason: str, *, root_id: str) -> None:
        """Cancel a task and recursively every descendant (§20).

        P1-20: execution cancellation runs for EVERY descendant, not only
        the root — a delegated child's task must not be marked cancelled
        while its processes remain alive.
        """
        # Resolve the complete tree before changing any status.  This closes
        # the race where a child remains alive because the root was transitioned
        # first and a later lookup no longer considered it runnable.
        task_ids = [task_id, *(await self._descendants_of(task_id))]
        for current in task_ids:
            self.set_token(current, reason)
        if self._exec is not None:
            for current in task_ids:
                try:
                    await self._exec.cancel_task(current)
                except Exception as exc:
                    _logger.warning(
                        "runtime cancel failed for task %s: %s", current, exc)
        for current in task_ids:
            await self._transition_status(current, TaskStatus.CANCELLED)

    async def _transition_status(self, task_id: str, status: TaskStatus) -> None:
        if self._tasks is None:
            return
        try:
            task = await self._tasks.get(task_id)
            if task is not None and _get_status(task) != status:
                await self._tasks.transition(task_id, status)
        except Exception as exc:
            _logger.warning(
                "cancel transition to %s failed for task %s: %s",
                status.value if hasattr(status, "value") else status,
                task_id,
                exc,
            )

    async def _descendants_of(self, task_id: str) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        frontier = [task_id]
        while frontier:
            nxt: list[str] = []
            for cur in frontier:
                for child in await self._children_of(cur):
                    if child not in seen:
                        seen.add(child)
                        nxt.append(child)
            out.extend(nxt)
            frontier = nxt
        return out

    async def _children_of(self, task_id: str) -> list[str]:
        if self._store is None:
            return []
        # A failed hierarchy lookup must not be interpreted as “no children”.
        # Doing that would let a database outage mark the parent cancelled
        # while leaving descendant processes alive.  Let the caller fail
        # closed so the cancellation request can be retried and the task is
        # not falsely reported as fully cancelled.
        rows = await self._store.list_children(task_id)
        return [r["id"] for r in rows]


def _get_status(task) -> TaskStatus:
    current = getattr(task, "status", None)
    if isinstance(current, TaskStatus):
        return current
    meta = getattr(task, "metadata", None) or {}
    raw = meta.get("status")
    if raw:
        try:
            return TaskStatus(raw)
        except ValueError:
            pass
    return TaskStatus.CREATED
