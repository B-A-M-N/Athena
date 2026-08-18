"""Unit tests for CancellationManager (BUILDSPEC §20)."""

from __future__ import annotations

import pytest

from athena.protocol.ids import new_id
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
        id=new_id("task"), objective=objective, session_id=session_id,
        parent_task_id=parent, resource_budget=ResourceBudget(),
    )


async def _create(manager, sessions, objective, *, parent=None):
    session_id = new_id("session")
    await sessions.create(session_id)
    return await manager.create(_spec(objective, session_id, parent=parent))


async def test_cancel_sets_the_token(env):
    manager, cancellations, sessions = env
    task = await _create(manager, sessions, "one")
    await cancellations.cancel(task.id)
    assert cancellations.is_cancelled(task.id) is True
    assert cancellations.token(task.id).is_set() is True


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