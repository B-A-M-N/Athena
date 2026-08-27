"""Scheduler -> Task -> Worker -> COMPLETE, with the occurrence marked FIRED."""
from __future__ import annotations
import pytest

from datetime import datetime, timedelta, timezone

from athena.protocol.ids import new_id
from athena.protocol.tasks import TaskStatus


async def _wait_terminal(svc, task_id, target=TaskStatus.COMPLETE.value, tries=200, delay=0.03):
    for _ in range(tries):
        if (await svc.get_task_status(task_id)) == target:
            return target
        from asyncio import sleep
        await sleep(delay)
    return await svc.get_task_status(task_id)


@pytest.mark.athena_claim("BHV-095")
@pytest.mark.athena_evidence("test", "e2e")
async def test_scheduled_job_fires_and_runs_to_complete(make_service):
    svc = await make_service()

    now = datetime.now(timezone.utc)
    due = (now - timedelta(seconds=10)).isoformat()
    job_id = "job-once-due"
    payload = {
        "template": {
            "objective": "SCHEDULED_2PLUS2 what is two plus two",
            "session_id": new_id("session"),
        }
    }
    trigger_spec = {"type": "once", "at": due}
    await svc._store_schedules.upsert_job(
        job_id, "once-due", payload=payload, trigger_spec=trigger_spec,
        enabled=True, next_run=due,
    )

    # Deterministic single tick (no waiting on the background loop interval).
    fired = await svc._scheduler.tick()
    assert fired == 1

    runs = await svc._db.fetch_all(
        "SELECT status, task_id FROM job_runs WHERE job_id = ?", (job_id,))
    assert len(runs) == 1
    assert runs[0]["status"] == "FIRED"
    task_id = runs[0]["task_id"]
    assert task_id is not None

    # The created task runs through the shared worker + kernel.
    assert await _wait_terminal(svc, task_id) == TaskStatus.COMPLETE.value

    # The ONCE trigger is exhausted: job disabled and next_run cleared.
    job = await svc._store_schedules.get_job(job_id)
    assert job is not None
    assert job.get("enabled") == 0
    assert not job.get("next_run")

    # Re-running a tick never double-fires the same occurrence.
    assert await svc._scheduler.tick() == 0


@pytest.mark.athena_claim("BHV-095")
@pytest.mark.athena_evidence("test", "e2e")
async def test_event_schedule_fires_once_from_canonical_event(make_service):
    svc = await make_service()
    job_id = "job-event-artifact"
    await svc._store_schedules.upsert_job(
        job_id,
        "artifact watcher",
        payload={"template": {"objective": "EVENT_SCHEDULED"}},
        trigger_spec={
            "type": "event",
            "event_name": "ArtifactCreated",
            "event_filters": {"kind": "report"},
            "times": 1,
        },
        enabled=True,
        next_run=None,
    )

    event = await svc._store_events.append_event(
        "ArtifactCreated", {"kind": "report", "uri": "artifact://report"}
    )
    assert event.id
    assert await svc._store_schedules.count_runs(job_id) == 1
    run = await svc._store_schedules.last_run(job_id)
    assert run is not None
    assert run["status"] == "FIRED"
    assert run["scheduled_for"] == f"event:{event.id}"

    # Re-delivery of the same canonical event is idempotent.
    assert await svc._scheduler.notify_event(event) == 0
    assert await svc._store_schedules.count_runs(job_id) == 1
    job = await svc._store_schedules.get_job(job_id)
    assert job is not None
    assert job["enabled"] is False
