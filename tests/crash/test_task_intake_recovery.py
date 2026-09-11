"""Ordinary CREATED task intake is reconciled by real service startup."""

from __future__ import annotations

import pytest

from athena.protocol.tasks import AgentRequest, TaskStatus
from athena.state.database import Database
from athena.state.tasks import TaskStore


@pytest.mark.athena_claim("RECOVERY-TASK-INTAKE")
@pytest.mark.athena_evidence("test", "crash-restart")
async def test_created_intake_reconciles_and_quarantines_unsafe_rows(
    make_durable_service, durable_db_path
):
    first = await make_durable_service(
        durable_db_path,
        scripts=None,
        worker_max_parallel=0,
    )
    request = AgentRequest(prompt="intake survives the restart", session_id="session-intake-real")
    spec = first._build_task_spec(request, request.session_id)
    created = await first._task_manager.create(spec)
    await first._record_canonical_user_turn(request, created)
    await first.stop()  # crash window: durable task/message, no queue transition

    db = Database(durable_db_path)
    await db._ensure_ready()
    try:
        # A task with no durable session cannot reconstruct a causal root and
        # must be quarantined rather than left inert in CREATED.
        await TaskStore(db).insert_task(
            "task-intake-unsafe",
            None,
            None,
            "unsafe intake",
            status=TaskStatus.CREATED,
        )
    finally:
        await db.close()

    second = await make_durable_service(
        durable_db_path,
        scripts=None,
        worker_max_parallel=0,
    )
    recovered = await second._store_tasks.get(created.id)
    quarantined = await second._store_tasks.get("task-intake-unsafe")
    assert recovered is not None
    assert recovered["status"] != TaskStatus.CREATED.value
    assert recovered["metadata"]["_intake_phase"] == "enqueued"
    assert quarantined is not None
    assert quarantined["status"] == TaskStatus.RECOVERY_REQUIRED.value
    health = second.startup_health()
    assert health["checks"]["task_intake"]["recovered"] == 1
    assert health["checks"]["task_intake"]["quarantined"] == 1
