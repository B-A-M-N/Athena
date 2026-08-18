"""Unit tests for DelegationManager (BUILDSPEC §69-73)."""

from __future__ import annotations

import pytest

from athena.protocol.ids import new_id
from athena.protocol.tasks import ResourceBudget, TaskSpec
from athena.state.database import Database
from athena.state.events import EventStore
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore
from athena.tasks.budgets import BudgetTracker
from athena.tasks.delegation import DelegationManager, DepthExceeded
from athena.tasks.manager import TaskManager


@pytest.fixture
async def env():
    db = Database(":memory:")
    await db._ensure_ready()
    sessions = SessionRepository(db)
    tasks = TaskStore(db)
    events = EventStore(db)
    budgets = BudgetTracker(task_store=tasks)
    manager = TaskManager(task_store=tasks, events=events, sessions=sessions, budgets=budgets)
    delegation = DelegationManager(task_manager=manager, budgets=budgets)
    yield manager, delegation, sessions
    await db.close()


def _spec(objective, session_id, *, budget=None, parent=None):
    return TaskSpec(
        id=new_id("task"), objective=objective, session_id=session_id,
        parent_task_id=parent, resource_budget=budget or ResourceBudget(),
    )


async def _create(manager, sessions, objective, *, budget=None, parent=None):
    session_id = new_id("session")
    await sessions.create(session_id)
    return await manager.create(_spec(objective, session_id, budget=budget, parent=parent))


async def _spawn(delegation, parent_id, objective="child work"):
    return await delegation.spawn_child(
        objective=objective, parent_task_id=parent_id
    )


async def test_spawn_child_sets_parent_task_id(env):
    manager, delegation, sessions = env
    parent = await _create(manager, sessions, "parent")
    child_id = await _spawn(delegation, parent.id)
    child = await manager.get(child_id)
    assert child.parent_task_id == parent.id
    assert child.objective == "child work"


async def test_max_child_depth_blocks_grandchildren(env):
    manager, delegation, sessions = env
    root = await _create(
        manager, sessions, "root",
        budget=ResourceBudget(max_child_depth=1, max_children=4),
    )
    child = await _create(manager, sessions, "child", parent=root.id)
    # A sibling of the child is fine (depth 1 from the root).
    sibling = await _spawn(delegation, root.id)
    assert (await manager.get(sibling)).parent_task_id == root.id
    # Delegating one more level below the child exceeds depth 1.
    with pytest.raises(DepthExceeded):
        await _spawn(delegation, child.id)


async def test_child_budget_derived_from_parent(env):
    manager, delegation, sessions = env
    parent = await _create(
        manager, sessions, "parent",
        budget=ResourceBudget(max_agent_iterations=5, max_child_depth=2),
    )
    child_id = await _spawn(delegation, parent.id)
    child = await manager.get(child_id)
    assert child.resource_budget is not None
    assert child.resource_budget.max_agent_iterations == 5
    # Merge semantics: the parent's depth ceiling transmits to the child.
    assert child.resource_budget.max_child_depth == 1