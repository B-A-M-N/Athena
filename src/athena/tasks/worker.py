from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

from athena.protocol.errors import RequestCancelled
from athena.protocol.tasks import TERMINAL_STATUSES, TaskResult, TaskStatus

__all__ = [
    "TaskWorker",
    "WorkerConfig",
]


@dataclass(frozen=True)
class WorkerConfig:
    """Worker tunables (BUILDSPEC §22 backpressure, §73 parallelism)."""

    max_parallel: int = 4
    max_retries: int = 0
    retryable_statuses: tuple[TaskStatus, ...] = (
        TaskStatus.INTERRUPTED,
    )
    poll_wait_s: float = 0.5


class TaskWorker:
    """A worker that pulls runnable tasks and drives them through the kernel.

    The worker has no reasoning loop of its own (INV-001/002). It atomically
    claims a schedulable task from the store, hands it to :meth:`AgentKernel.
    run_task`, and persists the resulting ``TaskResult`` through the task
    manager. Concurrency is bounded by an ``asyncio.Semaphore`` so spec §22
    (unbounded fanout forbidden) is honoured.
    """

    def __init__(
        self,
        *,
        task_manager: Any,
        kernel: Any,
        config: WorkerConfig | None = None,
        claim_statuses: tuple[TaskStatus, ...] | None = None,
    ) -> None:
        self._tasks = task_manager
        self._kernel = kernel
        self._config = config or WorkerConfig()
        self._claim_statuses = claim_statuses or (
            TaskStatus.QUEUED,
            TaskStatus.INTERRUPTED,
        )
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._worker_tasks: list[asyncio.Task] | None = None

    async def stop(self) -> None:
        """Signal the background loop to stop and await its graceful exit."""
        self._stop.set()
        tasks = getattr(self, '_worker_tasks', None) or ([] if self._task is None else [self._task])
        for t in tasks:
            try:
                await asyncio.wait_for(t, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                t.cancel()
        self._task = None
        self._worker_tasks = None

    # ------------------------------------------------------------------ #
    async def run_once(self) -> TaskResult | None:
        task_id = await self._claim(worker_id=f"run-once-{os.getpid()}")
        if task_id is None:
            return None
        result = await self._run_claimed(task_id)
        if await self._maybe_retry(task_id, result):
            await self._tasks.enqueue(task_id)
        return result

    async def run_task(self, task_id: str) -> TaskResult:
        """Drive the NAMED task through the kernel synchronously.

        Targets a specific task rather than racing the background loop for the
        next claimed task. The task is acquired by id (moving it to RUNNING) so
        a concurrent ``run_forever`` worker will not double-claim it under the
        ``claim_next`` status filter (``TaskStatus.QUEUED`` / ``INTERRUPTED``).
        """
        store = getattr(self._tasks, "_store", None)
        worker_id = f"manual-{os.getpid()}"
        if store is not None:
            try:
                await store.acquire_with_ownership(task_id, worker_id=worker_id)
            except Exception:
                raise  # Let IllegalStateTransition propagate
        else:
            await self._tasks.acquire(task_id)
        result = await self._run_claimed(task_id)
        if await self._maybe_retry(task_id, result):
            await self._tasks.enqueue(task_id)
        return result

    async def run_batch(self, limit: int = 1) -> list[TaskResult]:
        results: list[TaskResult] = []
        for _ in range(limit):
            result = await self.run_once()
            if result is None:
                break
            results.append(result)
        return results

    async def run_forever(self) -> None:
        workers = [
            asyncio.create_task(self._worker_loop(worker_id=i))
            for i in range(self._max_parallel)
        ]
        self._worker_tasks = workers
        try:
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            pass

    async def _worker_loop(self, *, worker_id: int) -> None:
        wid = f"worker-{worker_id}-{os.getpid()}"
        while not self._stop.is_set():
            task_id = await self._claim(worker_id=wid)
            if task_id is None:
                await asyncio.sleep(self._config.poll_wait_s)
                continue
            result = await self._run_claimed(task_id)
            if await self._maybe_retry(task_id, result):
                await self._tasks.enqueue(task_id)

    async def _maybe_retry(self, task_id: str, result: TaskResult) -> bool:
        if self._config.max_retries <= 0:
            return False
        if result.status not in self._config.retryable_statuses:
            return False
        if result.status in TERMINAL_STATUSES:
            return False
        count = await self._retry_count(task_id)
        if count >= self._config.max_retries:
            return False
        await self._set_retry_count(task_id, count + 1)
        return True

    async def _retry_count(self, task_id: str) -> int:
        row = await self._tasks.get(task_id)
        if row is None:
            return 0
        meta = row.metadata or {}
        try:
            return int(meta.get("worker_retries", 0))
        except (TypeError, ValueError):
            return 0

    async def _set_retry_count(self, task_id: str, count: int) -> None:
        store = getattr(self._tasks, "_store", None)
        if store is None:
            return
        await store.set_retry_count(task_id, count)

    async def _run_claimed(self, task_id: str) -> TaskResult:
        try:
            return await self._kernel.run_task(task_id)
        except RequestCancelled as exc:
            return await self._mark_failed(
                task_id, TaskStatus.CANCELLED, f"task cancelled: {exc}"
            )
        except Exception as exc:  # the kernel never crashes; classify truthfully.
            return await self._mark_failed(
                task_id, TaskStatus.FAILED, f"worker kernel failure: {exc}"
            )

    async def _mark_failed(self, task_id: str, status: TaskStatus, reason: str) -> TaskResult:
        try:
            return await self._tasks.finalize(
                task_id, status=status, reason=reason, summary=reason,
            )
        except Exception:
            pass
        return TaskResult(task_id=task_id, status=status, summary=reason)

    async def _claim(self, *, worker_id: str) -> str | None:
        store = getattr(self._tasks, "_store", None)
        if store is None:
            return None
        try:
            row = await store.claim_with_lease(self._claim_statuses, worker_id=worker_id)
        except Exception:
            return None
        if not row:
            return None
        return row.get("id")

    @property
    def _max_parallel(self) -> int:
        return max(1, self._config.max_parallel)