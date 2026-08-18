"""Hard crash recovery: tasks left RUNNING after a process KILL (not graceful stop).

These tests create a service, start a long-running task, then SIMULATE A CRASH
by closing the database connection without graceful shutdown (no stop() call).
They then restart on the same DB file and verify recovery works.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import pytest
from athena.protocol.tasks import AgentRequest, AutonomyLevel, TaskStatus
from athena.state.database import Database


# Helper to create a service with a durable DB
async def _make_service(db_path, workspace_dir, scripts=None):
    from athena.service.service import AthenaService
    from athena.service.config import AthenaConfig, ProviderConfig

    config = AthenaConfig(
        db_path=db_path,
        workspace_root=workspace_dir,
        artifact_root=os.path.join(workspace_dir, "artifacts"),
        providers=(ProviderConfig(kind="fake", name="fake", extra={"scripts": scripts or []}),),
    )
    svc = AthenaService(config=config)
    await svc.start()
    return svc


async def _simulate_crash(svc):
    """Simulate a hard crash: close the DB without graceful shutdown.

    This does NOT call stop() - it just closes the connection, leaving
    tasks in whatever state they were in (RUNNING).
    """
    if svc._db is not None:
        try:
            await svc._db._conn.close()
        except Exception:
            pass
        svc._db._conn = None
        svc._db._migrated = False
    svc._started = False


async def _wait_status(svc, task_id, target, tries=300, delay=0.02):
    from asyncio import sleep

    for _ in range(tries):
        status = await svc.get_task_status(task_id)
        if status == target:
            return status
        await sleep(delay)
    return await svc.get_task_status(task_id)


async def test_running_task_recovered_after_hard_crash():
    """A task left RUNNING by a hard crash is INTERRUPTED on restart."""
    tmpdir = tempfile.mkdtemp(prefix="athena-crash-")
    db_path = os.path.join(tmpdir, "test.db")

    sleep_script = [
        {"match": {"user_contains": "SLEEPLONG"},
         "respond": {"capability_call": {
             "capability_id": "execute",
             "arguments": {"language": "sh", "code": "sleep 30"},
         }}},
    ]

    svc1 = await _make_service(db_path, tmpdir, scripts=sleep_script)
    task = await svc1.submit(
        AgentRequest(prompt="SLEEPLONG now", autonomy=AutonomyLevel.AUTONOMOUS),
        wait=False,
    )
    assert await _wait_status(svc1, task.id, TaskStatus.RUNNING.value) == TaskStatus.RUNNING.value

    # HARD CRASH: close DB without graceful stop
    await _simulate_crash(svc1)

    # Verify task is still RUNNING in the DB (no graceful transition)
    db = Database(db_path)
    await db._ensure_ready()
    row = await db.fetch_one("SELECT status FROM tasks WHERE id = ?", (task.id,))
    await db.close()
    assert row["status"] == TaskStatus.RUNNING.value

    # Restart: recovery should transition RUNNING -> INTERRUPTED, then worker completes
    svc2 = await _make_service(db_path, tmpdir, scripts=None)
    # The task should eventually complete after recovery + worker re-claim
    status = await _wait_status(svc2, task.id, TaskStatus.COMPLETE.value, tries=500)
    assert status == TaskStatus.COMPLETE.value
    await svc2.stop()


async def test_worker_parallelism_real():
    """Four independent queued tasks actually overlap in execution."""
    from athena.service.service import AthenaService
    from athena.service.config import AthenaConfig, ProviderConfig

    tmpdir = tempfile.mkdtemp(prefix="athena-par-")
    db_path = os.path.join(tmpdir, "test.db")

    # Script that makes tasks sleep briefly so they overlap. The
    # capability_result_ok script terminal-completes once the execute call
    # succeeds, so each task performs a single ~1s execution and then stops.
    scripts = [
        {"match": {"capability_result_ok": True},
         "respond": {"text": "done", "done": True}},
        {"match": {"user_contains": "PAR"},
         "respond": {"capability_call": {
             "capability_id": "execute",
             "arguments": {"language": "sh", "code": "sleep 1"},
         }}},
    ]

    config = AthenaConfig(
        db_path=db_path,
        workspace_root=tmpdir,
        artifact_root=os.path.join(tmpdir, "artifacts"),
        providers=(ProviderConfig(kind="fake", name="fake", extra={"scripts": scripts}),),
        worker_max_parallel=4,
    )
    svc = AthenaService(config=config)
    await svc.start()

    import time

    start = time.monotonic()
    tasks = []
    for i in range(4):
        t = await svc.submit(
            AgentRequest(prompt=f"PAR test {i}", autonomy=AutonomyLevel.AUTONOMOUS),
            wait=False,
        )
        tasks.append(t)

    # Wait for all to complete
    for t in tasks:
        await _wait_status(svc, t.id, TaskStatus.COMPLETE.value, tries=500)
    elapsed = time.monotonic() - start

    # With real parallelism (4 workers, 4 tasks of ~1s each), total should be ~1-2s
    # With fake parallelism (1 at a time), it would be ~4s+
    # Allow generous margin for test environment
    assert elapsed < 3.5, f"Expected parallel execution (~1-2s), got {elapsed:.1f}s"
    await svc.stop()


async def test_run_task_rejects_already_claimed():
    """run_task on an already-RUNNING task should fail (ownership conflict)."""
    tmpdir = tempfile.mkdtemp(prefix="athena-claim-")
    db_path = os.path.join(tmpdir, "test.db")

    sleep_script = [
        {"match": {"user_contains": "SLEEP"},
         "respond": {"capability_call": {
             "capability_id": "execute",
             "arguments": {"language": "sh", "code": "sleep 30"},
         }}},
    ]
    svc = await _make_service(db_path, tmpdir, scripts=sleep_script)
    task = await svc.submit(
        AgentRequest(prompt="SLEEP now", autonomy=AutonomyLevel.AUTONOMOUS),
        wait=False,
    )
    await _wait_status(svc, task.id, TaskStatus.RUNNING.value)

    # Trying to run_task the same task should fail
    with pytest.raises(Exception):
        await svc.run_task(task.id)

    await svc.stop()