from decimal import Decimal

import pytest

from athena.protocol.artifacts import ArtifactRef
from athena.protocol.tasks import ContextRef, MutationRef, TaskSpec, TaskStatus, UsageSummary
from athena.state.database import Database
from athena.state.task_finalizations import TaskFinalizationStore
from athena.state.tasks import TaskStore
from athena.tasks.manager import TaskManager


@pytest.mark.asyncio
async def test_resource_recovery_commits_exact_pending_terminal_result_once():
    db = Database(":memory:")
    await db._ensure_ready()
    tasks = TaskStore(db)
    pending = TaskFinalizationStore(db)
    manager = TaskManager(task_store=tasks, finalizations=pending)
    observed = []

    async def observer(_task, result):
        observed.append(result)

    manager.add_finalize_observer(observer)
    task = TaskSpec(id="pending-finalization", objective="preserve result")
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
    intended = await manager.finalize(
        task,
        status=TaskStatus.PARTIAL,
        summary="partial but valid",
        evidence=(ContextRef(kind="artifact", ref="ctx-1", mime_type="text/plain"),),
        artifacts=(ArtifactRef(id="artifact-1", uri="artifact://sha256/abc", size=3),),
        mutations=(MutationRef(id="mutation-1", resource="file", operation="write"),),
        usage=UsageSummary(
            input_tokens=4,
            output_tokens=5,
            model_calls=1,
            cost_usd=Decimal("0.12"),
            duration_ms=9,
            executions=1,
            mutations=1,
        ),
    )

    assert intended.status is TaskStatus.RECOVERY_REQUIRED
    retained = await pending.get(task.id)
    assert retained is not None
    assert retained.result.status is TaskStatus.PARTIAL
    assert retained.result.summary == "partial but valid"
    assert retained.result.evidence[0].mime_type == "text/plain"
    assert retained.result.artifacts[0].uri == "artifact://sha256/abc"
    assert retained.result.mutations[0].id == "mutation-1"
    assert retained.result.usage.cost_usd == Decimal("0.12")

    recovered = await manager.commit_pending_finalization(task.id)

    assert recovered is not None
    assert recovered.status is TaskStatus.PARTIAL
    row = await tasks.get(task.id)
    assert row["status"] == TaskStatus.PARTIAL.value
    assert row["result_status"] == TaskStatus.PARTIAL.value
    assert row["summary"] == "partial but valid"
    assert len(observed) == 1
    assert observed[0].status is TaskStatus.PARTIAL
    assert await pending.get(task.id) is None
    await db.close()


@pytest.mark.asyncio
async def test_failed_finalize_observer_remains_replayable():
    db = Database(":memory:")
    await db._ensure_ready()
    tasks = TaskStore(db)
    pending = TaskFinalizationStore(db)
    manager = TaskManager(task_store=tasks, finalizations=pending)
    attempts = 0

    async def observer(_task, _result):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("projection unavailable")

    manager.add_finalize_observer(observer)
    task = TaskSpec(id="observer-replay", objective="replay observer")
    await manager.create(task)
    await manager.transition(task.id, TaskStatus.QUEUED)
    await manager.transition(task.id, TaskStatus.RUNNING)
    result = await manager.finalize(task, status=TaskStatus.COMPLETE, summary="done")

    retained = await pending.get(task.id)
    assert retained is not None
    assert retained.observer_state
    replayed = await manager.commit_pending_finalization(task.id)
    assert replayed is not None
    assert replayed.status is result.status
    assert attempts == 2
    assert await pending.get(task.id) is None
    await db.close()
