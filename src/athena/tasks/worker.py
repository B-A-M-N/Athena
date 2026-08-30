from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from athena.protocol.errors import PersistenceError, RequestCancelled
from athena.protocol.tasks import TERMINAL_STATUSES, TaskResult, TaskStatus

__all__ = [
    "TaskWorker",
    "WorkerConfig",
]

_logger = logging.getLogger("athena.tasks.worker")


@dataclass(frozen=True)
class WorkerConfig:
    """Worker tunables (BUILDSPEC §22 backpressure, §73 parallelism)."""

    max_parallel: int = 16
    max_retries: int = 0
    retryable_statuses: tuple[TaskStatus, ...] = (TaskStatus.INTERRUPTED,)
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
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._worker_tasks: list[asyncio.Task] | None = None
        self._consecutive_store_failures = 0
        self._last_store_error: str | None = None
        self._last_store_error_kind: str | None = None
        self._last_store_error_at: float | None = None
        self._claimed_count = 0
        self._completed_count = 0
        self._active_kernel_tasks: set[asyncio.Task] = set()

    async def stop(self) -> None:
        """Signal the background loop to stop and await its graceful exit."""
        self._stop.set()
        self._wake.set()
        tasks = getattr(self, "_worker_tasks", None) or ([] if self._task is None else [self._task])
        for t in tasks:
            try:
                await asyncio.wait_for(t, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                t.cancel()
        self._task = None
        self._worker_tasks = None

    def notify(self) -> None:
        """Wake same-process workers after durable work is enqueued."""
        self._wake.set()

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
            await store.acquire_with_ownership(task_id, worker_id=worker_id)
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
            asyncio.create_task(self._worker_loop(worker_id=i)) for i in range(self._max_parallel)
        ]
        self._worker_tasks = workers
        try:
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            pass

    async def _worker_loop(self, *, worker_id: int) -> None:
        wid = f"worker-{worker_id}-{os.getpid()}"
        while not self._stop.is_set():
            # Allow an already-running worker pool to be quiesced by updating
            # its config.  This is also the durable restart boundary: a
            # queued task must remain QUEUED while the operator has paused
            # claiming, even if the pool was started before the pause.
            if self._config.max_parallel <= 0:
                self._wake.clear()
                try:
                    await asyncio.wait_for(
                        self._wake.wait(), min(max(self._config.poll_wait_s, 0.01), 0.5)
                    )
                except TimeoutError:
                    pass
                continue
            self._wake.clear()
            task_id = await self._claim(worker_id=wid)
            if task_id is None:
                try:
                    await asyncio.wait_for(self._wake.wait(), self._config.poll_wait_s)
                except TimeoutError:
                    pass
                continue
            current = asyncio.current_task()
            if current is not None:
                self._active_kernel_tasks.add(current)
            try:
                result = await self._run_claimed(task_id)
                if await self._maybe_retry(task_id, result):
                    await self._tasks.enqueue(task_id)
                self._completed_count += 1
            except PersistenceError as exc:
                # A task whose terminal result could not be persisted is not
                # truthfully complete. Keep the worker alive and expose a
                # degraded health signal so operators/recovery can retry it.
                self._record_store_error("finalization", exc)
                await asyncio.sleep(self._failure_backoff())
            finally:
                if current is not None:
                    self._active_kernel_tasks.discard(current)

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
            return await self._mark_failed(task_id, TaskStatus.CANCELLED, f"task cancelled: {exc}")
        except Exception as exc:  # noqa: BLE001 - classify every kernel failure truthfully
            return await self._mark_failed(
                task_id, TaskStatus.FAILED, f"worker kernel failure: {exc}"
            )

    async def _mark_failed(self, task_id: str, status: TaskStatus, reason: str) -> TaskResult:
        try:
            return await self._tasks.finalize(
                task_id,
                status=status,
                reason=reason,
                summary=reason,
            )
        except Exception as exc:
            self._record_store_error("finalization", exc)
            raise PersistenceError(
                f"could not persist terminal result for task {task_id}: {exc}",
                cause=exc,
                task_id=task_id,
                intended_status=status.value,
            ) from exc

    async def _claim(self, *, worker_id: str) -> str | None:
        store = getattr(self._tasks, "_store", None)
        if store is None:
            self._record_store_error("claim", RuntimeError("task store is unavailable"))
            return None
        try:
            row = await store.claim_with_lease(self._claim_statuses, worker_id=worker_id)
        except Exception as exc:
            self._record_store_error("claim", exc)
            _logger.exception("task claim failed; queue health is degraded")
            return None
        if not row:
            self._record_store_recovered()
            return None
        task_id = row.get("id")
        if not task_id:
            self._record_store_error("claim", RuntimeError("claim returned no task id"))
            return None
        self._record_store_recovered()
        self._claimed_count += 1
        return task_id

    def health(self) -> dict[str, Any]:
        """Return queue persistence health for readiness/forensics surfaces."""
        degraded = self._consecutive_store_failures > 0
        return {
            "status": "degraded" if degraded else "ok",
            "consecutive_store_failures": self._consecutive_store_failures,
            "last_store_error": self._last_store_error,
            "last_store_error_kind": self._last_store_error_kind,
            "claimed": self._claimed_count,
            "completed": self._completed_count,
        }

    def _record_store_error(self, kind: str, error: BaseException) -> None:
        self._consecutive_store_failures += 1
        self._last_store_error_kind = kind
        self._last_store_error = str(error)
        self._last_store_error_at = asyncio.get_running_loop().time()
        if _database_is_closed(error):
            # A closed connection is not a retryable outage for this worker.
            # In production it means the owning process is being torn down;
            # in crash-recovery tests it represents the same boundary. Stop
            # claiming and cancel any in-flight kernel work so a restarted
            # service is the only owner left to reconcile the task.
            self._stop.set()
            current = asyncio.current_task()
            for task in tuple(self._active_kernel_tasks):
                if task is not current and not task.done():
                    task.cancel()

    def _record_store_recovered(self) -> None:
        self._consecutive_store_failures = 0

    def _failure_backoff(self) -> float:
        return min(
            max(self._config.poll_wait_s, 0.1)
            * (2 ** min(self._consecutive_store_failures - 1, 5)),
            30.0,
        )

    @property
    def _max_parallel(self) -> int:
        # Zero is a supported paused-worker state used during durable restart
        # tests and operator quiescing.  A negative value remains invalid but
        # must not accidentally create a worker either.
        return max(0, self._config.max_parallel)


def _database_is_closed(error: BaseException) -> bool:
    """Recognize terminal connection loss without masking other DB failures."""
    text = str(error).casefold()
    return "closed database" in text or "no active connection" in text
