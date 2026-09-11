"""Control-plane startup must not activate ambient work producers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from athena.service.service import AthenaService
from athena.protocol.tasks import AgentRequest


@pytest.mark.asyncio
async def test_control_plane_start_leaves_queued_work_and_due_scheduler_idle():
    service = AthenaService.in_memory()
    await service.start(activate_runtime=False)
    try:
        due = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        await service._store_schedules.upsert_job(
            "job-control-plane-due",
            "must not fire during inspection",
            payload={"template": {"objective": "must remain unfired"}},
            trigger_spec={"type": "once", "at": due},
            enabled=True,
            next_run=due,
        )
        task = await service.submit(
            AgentRequest(prompt="remain queued"),
            wait=False,
        )
        before = await service.get_task_status(task.id)
        await asyncio.sleep(0.05)
        after = await service.get_task_status(task.id)

        assert before == after == "QUEUED"
        assert service._worker_task is None
        assert service._scheduler is not None
        assert service._scheduler.is_running() is False
        assert service._watch_poll_task is None
        assert service._runtime_active is False
        assert await service._store_schedules.count_runs("job-control-plane-due") == 0
        job = await service._store_schedules.get_job("job-control-plane-due")
        assert job is not None
        assert job["next_run"] == due
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_runtime_activation_is_explicit_after_control_plane_start():
    service = AthenaService.in_memory()
    await service.start(activate_runtime=False)
    try:
        await service.activate_runtime()
        assert service._runtime_active is True
        assert service._worker_task is not None
        assert service._scheduler is not None
        assert service._scheduler.is_running() is True
    finally:
        await service.stop()
