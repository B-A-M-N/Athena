from athena.protocol.tasks import TaskSpec, TaskStatus
from athena.state.database import Database
from athena.state.tasks import TaskStore
from athena.tasks.manager import TaskManager


async def test_finalization_barrier_parks_unproven_cleanup_as_recovery():
    db = Database(":memory:")
    await db._ensure_ready()
    store = TaskStore(db)
    manager = TaskManager(task_store=store)
    task = TaskSpec(id="task-barrier", objective="finish safely")
    await manager.create(task)
    await manager.transition(task.id, TaskStatus.QUEUED)
    await manager.transition(task.id, TaskStatus.RUNNING)

    async def barrier(_task, _result):
        return {
            "confirmed": False,
            "unresolved": [{"resource_type": "terminal", "resource_id": "pty-1"}],
            "failures": [{"resource": "terminal", "error": "still alive"}],
        }

    manager.set_finalization_barrier(barrier)
    result = await manager.finalize(task, status=TaskStatus.COMPLETE, summary="done")

    assert result.status is TaskStatus.RECOVERY_REQUIRED
    row = await store.get(task.id)
    assert row["status"] == TaskStatus.RECOVERY_REQUIRED.value
    assert row["completed_at"] is None
    assert row["metadata"]["recovery_required"] is True
    assert row["metadata"]["recovery_markers"][-1]["intended_status"] == "COMPLETE"
    await db.close()
