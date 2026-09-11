"""The generic resume operation owns only interrupted and queued tasks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from athena.protocol.tasks import TaskStatus
from athena.service.interaction import OperatorInteractionService
from athena.state.database import Database
from athena.state.tasks import TaskStore
from athena.tasks.manager import TaskManager


@pytest.mark.asyncio
async def test_resume_accepts_only_interrupted_and_queued_tasks() -> None:
    db = Database(":memory:")
    await db._ensure_ready()
    try:
        store = TaskStore(db)
        manager = TaskManager(task_store=store)
        service = SimpleNamespace(_store_tasks=store, _task_manager=manager)

        async def get_task(task_id: str):
            return await manager.get(task_id)

        service.get_task = get_task
        statuses = (
            TaskStatus.INTERRUPTED,
            TaskStatus.QUEUED,
            TaskStatus.CREATED,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.WAITING_INPUT,
            TaskStatus.BLOCKED,
            TaskStatus.RECOVERY_REQUIRED,
            TaskStatus.FAILED,
        )
        for status in statuses:
            task_id = f"task-resume-{status.value.lower()}"
            await store.insert_task(task_id, None, None, status.value, status=status)

        interaction = OperatorInteractionService(service)
        resumed = await interaction.resume_task("task-resume-interrupted")
        assert resumed.metadata["status"] == TaskStatus.QUEUED.value

        queued = await interaction.resume_task("task-resume-queued")
        assert queued.metadata["status"] == TaskStatus.QUEUED.value

        for status in statuses[2:]:
            with pytest.raises(ValueError, match=status.value):
                await interaction.resume_task(f"task-resume-{status.value.lower()}")
    finally:
        await db.close()
