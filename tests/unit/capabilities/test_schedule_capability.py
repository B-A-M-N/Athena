"""Behavioral coverage for scheduler capability management operations."""

from __future__ import annotations

import pytest

from athena.capabilities.schedule import ScheduleAPI
from athena.scheduler.scheduler import TriggerType
from athena.state.database import Database
from athena.state.schedules import ScheduleStore


class _Scheduler:
    def __init__(self, store):
        self._store = store


async def _api():
    db = Database(":memory:")
    await db._ensure_ready()
    return db, ScheduleAPI(_Scheduler(ScheduleStore(db)), task_manager=None)


async def test_schedule_api_round_trips_jobs_and_enforces_owner_visibility():
    db, api = await _api()
    owner = {"task_id": "task-a", "session_id": "session-a", "project_id": "repo-a"}
    other = {"task_id": "task-b", "session_id": "session-b", "project_id": "repo-b"}

    created = await api.create(
        name="nightly verification",
        objective="verify the next release",
        trigger={"type": "interval", "interval_seconds": 60},
        session_id="session-a",
        owner=owner,
    )
    job_id = created["job_id"]

    job = await api.inspect(job_id, owner=owner)
    assert job is not None
    assert job["trigger"]["type"] == TriggerType.INTERVAL.value
    assert job["template"]["objective"] == "verify the next release"
    assert job["metadata"]["_owner"] == owner
    assert job["next_run"]

    assert len(await api.list_jobs(owner=owner)) == 1
    assert await api.list_jobs(owner=other) == []
    assert await api.inspect(job_id, owner=other) is None
    assert await api.disable(job_id, owner=other) is False
    assert await api.delete(job_id, owner=other) is False

    assert await api.disable(job_id, owner=owner) is True
    assert len(await api.list_jobs(owner=owner)) == 1
    assert await api.enable(job_id, owner=owner) is True
    assert await api.delete(job_id, owner=owner) is True
    assert await api.delete(job_id, owner=owner) is False
    await db.close()


@pytest.mark.parametrize(
    ("trigger", "message"),
    [
        ({"type": "once"}, "requires at"),
        ({"type": "interval", "interval_seconds": 0}, "must be positive"),
        ({"type": "cron", "cron": "* * *"}, "five fields"),
        ({"type": "event"}, "requires event_name"),
        ({"type": "once", "at": "not-a-date"}, "ISO-8601"),
    ],
)
async def test_schedule_api_rejects_incomplete_trigger_contract(trigger, message):
    db, api = await _api()
    with pytest.raises(ValueError, match=message):
        await api.create(
            name="invalid",
            objective="must fail",
            trigger=trigger,
            owner={"task_id": "task-a"},
        )
    await db.close()

