"""Unit tests for CancellationManager (BUILDSPEC §20)."""

from __future__ import annotations

import pytest

from athena.protocol.ids import new_id
from athena.protocol.errors import CancellationUncertain
from athena.protocol.tasks import ResourceBudget, TaskSpec, TaskStatus
from athena.state.database import Database
from athena.state.events import EventStore
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore
from athena.tasks.cancellation import CancellationManager
from athena.tasks.manager import TaskManager


@pytest.fixture
async def env():
    db = Database(":memory:")
    await db._ensure_ready()
    sessions = SessionRepository(db)
    tasks = TaskStore(db)
    events = EventStore(db)
    manager = TaskManager(task_store=tasks, events=events, sessions=sessions)
    cancellations = CancellationManager(task_manager=manager, task_store=tasks)
    yield manager, cancellations, sessions
    await db.close()


def _spec(objective, session_id, *, parent=None):
    return TaskSpec(
        id=new_id("task"),
        objective=objective,
        session_id=session_id,
        parent_task_id=parent,
        resource_budget=ResourceBudget(),
    )


async def _create(manager, sessions, objective, *, parent=None):
    session_id = new_id("session")
    await sessions.create(session_id)
    return await manager.create(_spec(objective, session_id, parent=parent))


@pytest.mark.athena_claim("BHV-076")
@pytest.mark.athena_evidence("test", "invariant")
async def test_cancel_sets_the_token(env):
    manager, cancellations, sessions = env
    task = await _create(manager, sessions, "one")
    await cancellations.cancel(task.id)
    assert cancellations.is_cancelled(task.id) is True
    assert cancellations.token(task.id).is_set() is True


@pytest.mark.athena_claim("BHV-075", "BHV-076")
@pytest.mark.athena_evidence("test", "invariant")
async def test_cancel_propagates_to_children(env):
    manager, cancellations, sessions = env
    parent = await _create(manager, sessions, "parent")
    child = await _create(manager, sessions, "child", parent=parent.id)
    grandchild = await _create(manager, sessions, "grandchild", parent=child.id)

    await cancellations.cancel(parent.id)

    assert cancellations.is_cancelled(parent.id) is True
    assert cancellations.is_cancelled(child.id) is True
    assert cancellations.is_cancelled(grandchild.id) is True
    for t in (parent, child, grandchild):
        row = await manager.get(t.id)
        assert row.metadata["status"] == TaskStatus.CANCELLED.value


@pytest.mark.athena_claim("BHV-076")
@pytest.mark.athena_evidence("test", "invariant")
async def test_cancel_propagates_to_execution_manager_for_every_descendant(env):
    manager, cancellations, sessions = env

    class ExecutionSpy:
        def __init__(self):
            self.cancelled = []

        async def cancel_task(self, task_id):
            self.cancelled.append(task_id)

    spy = ExecutionSpy()
    cancellations._exec = spy
    parent = await _create(manager, sessions, "parent")
    child = await _create(manager, sessions, "child", parent=parent.id)
    grandchild = await _create(manager, sessions, "grandchild", parent=child.id)

    await cancellations.cancel(parent.id)

    assert spy.cancelled == [parent.id, child.id, grandchild.id]


@pytest.mark.athena_claim("BHV-023")
@pytest.mark.athena_evidence("test", "invariant")
async def test_interrupt_sets_interrupted_recoverable(env):
    manager, cancellations, sessions = env
    task = await _create(manager, sessions, "interrupt me")
    await manager.acquire(task.id)

    status = await cancellations.interrupt(task.id)
    assert status == TaskStatus.INTERRUPTED
    row = await manager.get(task.id)
    assert row.metadata["status"] == TaskStatus.INTERRUPTED.value
    # Interrupt is recoverable: it must NOT set the terminal cancel token.
    assert cancellations.is_cancelled(task.id) is False


async def test_runtime_failure_never_reports_cancelled(env):
    manager, cancellations, sessions = env

    class FailingExecution:
        async def cancel_task(self, task_id):
            raise RuntimeError(f"runtime {task_id} refused termination")

    cancellations._exec = FailingExecution()
    task = await _create(manager, sessions, "uncertain")

    with pytest.raises(CancellationUncertain):
        await cancellations.cancel(task.id)

    row = await manager.get(task.id)
    assert row.metadata["status"] == TaskStatus.RECOVERY_REQUIRED.value
    assert cancellations.proof(task.id).runtime_cancel_confirmed is False


async def test_cancel_of_final_task_preserves_actual_status(env):
    manager, cancellations, sessions = env
    task = await _create(manager, sessions, "already complete")
    await manager.acquire(task.id)
    await manager.finalize(task, status=TaskStatus.COMPLETE, summary="done")

    assert await cancellations.cancel(task.id) == TaskStatus.COMPLETE
    assert (await manager.get(task.id)).metadata["status"] == TaskStatus.COMPLETE.value


async def test_uncertain_cancellation_can_reconcile_on_retry(env):
    manager, cancellations, sessions = env

    class FlakyExecution:
        def __init__(self):
            self.fail = True

        async def cancel_task(self, task_id):
            if self.fail:
                raise RuntimeError("still alive")
            return True

    execution = FlakyExecution()
    cancellations._exec = execution
    task = await _create(manager, sessions, "retry")

    with pytest.raises(CancellationUncertain):
        await cancellations.cancel(task.id)
    assert (await manager.get(task.id)).metadata["status"] == TaskStatus.RECOVERY_REQUIRED.value

    execution.fail = False
    assert await cancellations.cancel(task.id) == TaskStatus.CANCELLED
    assert (await manager.get(task.id)).metadata["status"] == TaskStatus.CANCELLED.value


async def test_transition_persistence_failure_is_not_swallowed(env, monkeypatch):
    manager, cancellations, sessions = env
    task = await _create(manager, sessions, "persistence")

    async def fail_transition(*args, **kwargs):
        raise RuntimeError("database write failed")

    monkeypatch.setattr(manager, "transition", fail_transition)
    with pytest.raises(RuntimeError, match="database write failed"):
        await cancellations.cancel(task.id)


async def test_descendant_runtime_failure_keeps_root_recoverable(env):
    manager, cancellations, sessions = env

    class FailingChildExecution:
        async def cancel_task(self, task_id):
            if task_id == child.id:
                raise RuntimeError("child process still alive")
            return True

    parent = await _create(manager, sessions, "parent")
    child = await _create(manager, sessions, "child", parent=parent.id)
    cancellations._exec = FailingChildExecution()

    with pytest.raises(CancellationUncertain):
        await cancellations.cancel(parent.id)

    assert (await manager.get(parent.id)).metadata["status"] == TaskStatus.RECOVERY_REQUIRED.value
    assert (await manager.get(child.id)).metadata["status"] == TaskStatus.RECOVERY_REQUIRED.value


async def test_cancel_tree_does_not_call_runtime_for_final_descendant(env):
    manager, cancellations, sessions = env

    class ExecutionSpy:
        def __init__(self):
            self.cancelled = []

        async def cancel_task(self, task_id):
            self.cancelled.append(task_id)

    spy = ExecutionSpy()
    cancellations._exec = spy
    parent = await _create(manager, sessions, "parent")
    child = await _create(manager, sessions, "already done", parent=parent.id)
    await manager.acquire(child.id)
    await manager.finalize(child, status=TaskStatus.COMPLETE, summary="done")

    await cancellations.cancel(parent.id)

    assert spy.cancelled == [parent.id]
    assert (await manager.get(child.id)).metadata["status"] == TaskStatus.COMPLETE.value
