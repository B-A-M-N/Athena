"""Scheduler crash recovery: occurrences must not get stuck CLAIMED.

If Athena dies mid-fire (between creating the Task and marking the occurrence
FIRED), restart reconciliation must FIRED the occurrence (a matching Task
exists via its deterministic ``_occurrence`` metadata key) or release it. A job
that never fired must fire normally after restart.
"""
from __future__ import annotations
import pytest

from datetime import datetime, timedelta, timezone

from athena.protocol.tasks import TaskStatus
from athena.protocol.ids import new_id
from athena.state.database import Database


def _due_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()


async def _read_run(db_path, job_id) -> dict | None:
    db = Database(db_path)
    await db._ensure_ready()
    try:
        rows = await db.fetch_all(
            "SELECT status, task_id, id FROM job_runs WHERE job_id = ? "
            "ORDER BY started_at ASC",
            (job_id,),
        )
        return rows[-1] if rows else None
    finally:
        await db.close()


@pytest.mark.athena_claim("BHV-098")
@pytest.mark.athena_evidence("test", "e2e")
@pytest.mark.athena_scenario("RECOVERY-003")
async def test_claimed_occurrence_reconciled_to_fired_on_restart(
    make_durable_service, durable_db_path
):
    """A CLAIMED occurrence whose Task exists is FIRED on restart, not stuck."""
    job_id = "job-midfire-crash"
    due = _due_iso()
    token = new_id("sess")

    svc1 = await make_durable_service(durable_db_path, scripts=None)
    await svc1._store_schedules.upsert_job(
        job_id, "midfire", payload={
            "template": {"objective": "SCHED_MIDFIRE", "session_id": token},
        },
        trigger_spec={"type": "once", "at": due},
        enabled=True, next_run=due,
    )
    await svc1.stop()  # never ticked, nothing claimed yet

    # Simulate the crash window: the scheduler CLAIMED the occurrence and
    # created the Task, then died before complete_claim() marked it fired.
    svc2 = await make_durable_service(durable_db_path, scripts=None)
    claim = await svc2._store_schedules.claim_next_due(job_id, due)
    assert claim, "occurrence not claimable"
    created = await svc2._task_manager.create(_occurrence_task(job_id, due, token))
    await svc2._task_manager.enqueue(created.id)
    await svc2.stop()  # hard stop before complete_claim

    pre = await _read_run(durable_db_path, job_id)
    assert pre is not None and pre["status"] == "CLAIMED"

    # Restart: reconcile must match the created Task (via _occurrence metadata)
    # and promote the CLAIMED occurrence to FIRED.
    svc3 = await make_durable_service(durable_db_path, scripts=None)
    await svc3._scheduler.reconcile()
    run = await _read_run(durable_db_path, job_id)
    assert run is not None
    assert run["status"] == "FIRED", f"occurrence stuck {run['status']!r}"
    assert run["task_id"], "occurrence FIRED but has no linked task"


async def test_job_not_fired_before_stop_fires_after_restart(
    make_durable_service, durable_db_path
):
    """A scheduled job stopped before firing runs normally after restart."""
    job_id = "job-never-fired"
    due = _due_iso()
    token = new_id("session")

    svc1 = await make_durable_service(durable_db_path, scripts=None)
    await svc1._store_schedules.upsert_job(
        job_id, "never-fired", payload={
            "template": {"objective": "SCHED_FIRED_OK", "session_id": token},
        },
        trigger_spec={"type": "once", "at": due},
        enabled=True, next_run=due,
    )
    await svc1.stop()  # stopped before it fired

    assert await _read_run(durable_db_path, job_id) is None

    svc2 = await make_durable_service(durable_db_path, scripts=None)
    fired = await svc2._scheduler.tick()
    assert fired == 1
    run = await _read_run(durable_db_path, job_id)
    assert run is not None
    assert run["status"] == "FIRED", run["status"]
    assert run["task_id"], "fired occurrence missing a linked task"
    status = await svc2.get_task_status(run["task_id"])
    assert status in (TaskStatus.COMPLETE.value, TaskStatus.RUNNING.value,
                      TaskStatus.QUEUED.value), status


def _occurrence_task(job_id: str, scheduled_for: str, session_id: str):
    """Build a TaskSpec matching the scheduler's deterministic occurrence key."""
    from athena.protocol.tasks import TaskSpec
    return TaskSpec(
        id=new_id("task"),
        objective="SCHED_MIDFIRE",
        session_id=session_id,
        metadata={"_occurrence": f"{job_id}|{scheduled_for}"},
    )