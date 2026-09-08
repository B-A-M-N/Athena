from __future__ import annotations

import pytest

from athena.protocol.tasks import TaskStatus
from athena.scheduler.scheduler import Scheduler
from athena.state.database import Database
from athena.state.events import EventStore
from athena.state.schedules import ScheduleStore
from athena.state.tasks import TaskStore
from athena.tasks.manager import TaskManager


@pytest.mark.asyncio
async def test_reconcile_enqueues_created_occurrence_before_firing_claim() -> None:
    db = Database(":memory:")
    await db._ensure_ready()
    try:
        schedules = ScheduleStore(db)
        tasks = TaskStore(db)
        manager = TaskManager(task_store=tasks, events=EventStore(db))
        scheduled_for = "2026-01-01T00:00:00+00:00"
        await schedules.upsert_job(
            "job-recovery",
            "recovery",
            payload={"template": {"objective": "recover"}},
            trigger_spec={"type": "once", "at": scheduled_for},
            next_run=scheduled_for,
        )
        claim = await schedules.claim_next_due("job-recovery", scheduled_for)
        assert claim is not None
        await tasks.insert_task(
            "task-recovery",
            None,
            None,
            "recover",
            metadata={"_occurrence": f"job-recovery|{scheduled_for}"},
            status=TaskStatus.CREATED,
        )

        scheduler = Scheduler(schedules, manager)
        await scheduler.reconcile()

        task = await tasks.get("task-recovery")
        run = await schedules.last_run("job-recovery")
        job = await schedules.get_job("job-recovery")
        assert task["status"] == TaskStatus.QUEUED.value
        assert run["status"] == "FIRED"
        assert run["task_id"] == "task-recovery"
        assert job["next_run"] is None
    finally:
        await db.close()
