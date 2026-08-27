from __future__ import annotations

import pytest

from athena.protocol.errors import PersistenceError
from athena.protocol.tasks import TaskStatus
from athena.tasks.worker import TaskWorker


class _BrokenStore:
    async def claim_with_lease(self, statuses, *, worker_id):
        raise RuntimeError("database unavailable")


class _Tasks:
    def __init__(self, store=None, *, finalize_error=None):
        self._store = store
        self.finalize_error = finalize_error

    async def finalize(self, *args, **kwargs):
        if self.finalize_error is not None:
            raise self.finalize_error
        raise AssertionError("test task manager should not finalize")


async def _never_run(_task_id):
    raise AssertionError("kernel should not run")


@pytest.mark.asyncio
async def test_claim_store_failure_is_visible_as_degraded_health():
    worker = TaskWorker(task_manager=_Tasks(_BrokenStore()), kernel=_never_run)

    assert await worker._claim(worker_id="test-worker") is None
    health = worker.health()
    assert health["status"] == "degraded"
    assert health["consecutive_store_failures"] == 1
    assert health["last_store_error_kind"] == "claim"
    assert "database unavailable" in health["last_store_error"]


@pytest.mark.asyncio
async def test_terminal_persistence_failure_is_not_fabricated_as_task_result():
    worker = TaskWorker(
        task_manager=_Tasks(finalize_error=RuntimeError("write failed")),
        kernel=_never_run,
    )

    with pytest.raises(PersistenceError):
        await worker._mark_failed("task-1", TaskStatus.FAILED, "kernel failed")
    assert worker.health()["status"] == "degraded"
    assert worker.health()["last_store_error_kind"] == "finalization"
