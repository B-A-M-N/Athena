"""Crash recovery: tasks left mid-flight when Athena stops hard.

A service stopped before a task completes must leave the task INTERRUPTED,
never CANCELLED or lost; a QUEUED task must survive restart. All of these use
a real (file-backed) DB so state persists across the simulated crash.
"""
from __future__ import annotations
import pytest

from athena.protocol.tasks import AgentRequest, AutonomyLevel, TaskStatus
from athena.state.database import Database
from athena.state.mutations import PLANNED


_SLEEP_SCRIPT = (
    {"match": {"user_contains": "SLEEPLONG"},
     "respond": {"capability_call": {
         "capability_id": "execute",
         "arguments": {"language": "sh", "code": "sleep 30"},
     }}},
)


async def _wait_status(svc, task_id, target, tries=300, delay=0.02):
    from asyncio import sleep

    for _ in range(tries):
        if (await svc.get_task_status(task_id)) == target:
            return target
        await sleep(delay)
    return await svc.get_task_status(task_id)


async def _read_task(db_path, task_id) -> dict:
    db = Database(db_path)
    await db._ensure_ready()
    try:
        row = await db.fetch_one("SELECT id, status FROM tasks WHERE id = ?", (task_id,))
        return row or {}
    finally:
        await db.close()


@pytest.mark.athena_claim("BHV-080")
@pytest.mark.athena_evidence("test", "e2e")
async def test_running_task_becomes_interrupted_on_hard_stop(
    make_durable_service, durable_db_path
):
    """A RUNNING task is INTERRUPTED (not CANCELLED, not lost) on restart."""
    svc1 = await make_durable_service(durable_db_path, scripts=_SLEEP_SCRIPT)
    task = await svc1.submit(
        AgentRequest(prompt="SLEEPLONG now", autonomy=AutonomyLevel.AUTONOMOUS),
        wait=False,
    )
    # Drive until the task is actively RUNNING (its execute call is in flight).
    assert await _wait_status(svc1, task.id, TaskStatus.RUNNING.value) == TaskStatus.RUNNING.value

    # Simulate a crash: stop the service (graceful path parks in-flight work).
    await svc1.stop()
    row = await _read_task(durable_db_path, task.id)
    assert row["status"] == TaskStatus.INTERRUPTED.value

    # Restart on the same DB; the background worker reclaims the INTERRUPTED
    # task (claim_statuses includes INTERRUPTED) and drives it to COMPLETE.
    svc2 = await make_durable_service(durable_db_path, scripts=None)
    assert await _wait_status(svc2, task.id, TaskStatus.COMPLETE.value) == TaskStatus.COMPLETE.value


@pytest.mark.athena_claim("BHV-079")
@pytest.mark.athena_evidence("test", "e2e")
async def test_queued_task_survives_restart(make_durable_service, durable_db_path):
    """A QUEUED (not yet claimed) task is still queued and runs after restart."""
    svc1 = await make_durable_service(durable_db_path, scripts=None)
    # Freeze the worker so it cannot claim the task before we stop the service.
    from athena.tasks.worker import WorkerConfig

    svc1._worker._config = WorkerConfig(poll_wait_s=3600, max_parallel=0)
    task = await svc1.submit(
        AgentRequest(prompt="QUEUED_SURVIVES restart", autonomy=AutonomyLevel.AUTONOMOUS),
        wait=False,
    )
    status = await svc1.get_task_status(task.id)
    assert status == TaskStatus.QUEUED.value, f"expected QUEUED, got {status!r}"
    await svc1.stop()

    # Restart with a live worker; the queued task is picked up and completes.
    svc2 = await make_durable_service(durable_db_path, scripts=None)
    assert await _wait_status(svc2, task.id, TaskStatus.COMPLETE.value) == TaskStatus.COMPLETE.value


@pytest.mark.athena_claim("BHV-079")
@pytest.mark.athena_evidence("test", "e2e")
async def test_mutation_intent_wal_survives_crash(make_durable_service, durable_db_path):
    """A PLANNED write-ahead intent is durable AND reconciled on restart.

    The WAL row survives, but startup recovery no longer leaves it PLANNED:
    a PLANNED intent whose side effect never began is marked FAILED (the
    effect definitively did not happen), while a STARTED intent becomes
    RECOVERY_REQUIRED (the effect may or may not have happened).
    """
    from athena.state.mutations import FAILED as MUT_FAILED, RECOVERY_REQUIRED

    svc1 = await make_durable_service(durable_db_path, scripts=None)
    task = await svc1.submit(
        AgentRequest(prompt="MUTWAL plan", autonomy=AutonomyLevel.AUTONOMOUS),
        wait=True,
    )
    # Persist intents in both pre-effect states: PLANNED (never started) and
    # STARTED (effect possibly mid-flight when the crash hit).
    planned_id = await svc1._store_mutations.record_intent(
        task_id=task.id,
        resource="crash-probe.txt",
        operation="write",
        inverse={"op": "delete", "target": "crash-probe.txt"},
        metadata={"capability_id": "fs"},
    )
    started_id = await svc1._store_mutations.record_intent(
        task_id=task.id,
        resource="crash-probe-started.txt",
        operation="write",
        metadata={"capability_id": "fs"},
    )
    await svc1._store_mutations.mark_started(started_id)
    assert planned_id and started_id
    await svc1.stop()

    # Restart on the same DB: recovery reconciles both intents.
    svc2 = await make_durable_service(durable_db_path, scripts=None)
    tables = await svc2._db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='mutations'")
    assert tables, "mutations WAL table missing after restart"
    rows = await svc2._store_mutations.list_for_task(task.id)
    by_id = {r["id"]: r for r in rows}
    assert planned_id in by_id, f"PLANNED intent lost across restart: {rows!r}"
    assert started_id in by_id, f"STARTED intent lost across restart: {rows!r}"

    planned_row = by_id[planned_id]
    assert planned_row["status"] == MUT_FAILED, (
        f"PLANNED intent should be reconciled to FAILED, got "
        f"{planned_row['status']!r}"
    )
    assert planned_row["resource"] == "crash-probe.txt"
    assert planned_row["metadata"]["capability_id"] == "fs"

    started_row = by_id[started_id]
    assert started_row["status"] == RECOVERY_REQUIRED, (
        f"STARTED intent should be reconciled to RECOVERY_REQUIRED, got "
        f"{started_row['status']!r}"
    )