"""Runtime complexity survives restart by replaying canonical event facts."""

from __future__ import annotations

import pytest

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.runtime_escalation import ComplexityLedger
from athena.protocol.events import make_event
from athena.protocol.ids import new_id
from athena.protocol.tasks import ResourceBudget, TaskSpec
from athena.state.database import Database
from athena.state.events import EventStore
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore
from athena.tasks.manager import TaskManager


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database._ensure_ready()
    yield database
    await database.close()


async def _task(events: EventStore) -> TaskSpec:
    db = events._db
    sessions = SessionRepository(db)
    manager = TaskManager(task_store=TaskStore(db), events=events, sessions=sessions)
    session_id = new_id("session")
    await sessions.create(session_id)
    spec = TaskSpec(
        id=new_id("task"),
        objective="reconstruct complexity",
        session_id=session_id,
        resource_budget=ResourceBudget(),
    )
    await manager.create(spec)
    return spec


async def test_mutation_then_execute_events_are_reconstructed_after_restart(db):
    events = EventStore(db)
    task = await _task(events)
    await events.append_event(
        "CapabilityRequested",
        {"call_id": "call-1", "capability_id": "fs", "arguments": {"path": "x"}},
        task_id=task.id,
    )
    await events.append_event(
        "MutationPrepared",
        {"call_id": "call-1", "capability_id": "fs", "resource": "x", "operation": "write"},
        task_id=task.id,
    )
    await events.append_event(
        "CapabilityRequested",
        {"call_id": "call-2", "capability_id": "execute", "arguments": {}},
        task_id=task.id,
    )
    await events.append_event(
        "MutationPrepared",
        {"call_id": "call-2", "capability_id": "execute", "resource": "", "operation": "execute"},
        task_id=task.id,
    )

    # Simulated restart: a fresh dispatcher replays durable canonical events.
    dispatcher = CapabilityDispatcher.__new__(CapabilityDispatcher)
    dispatcher._complexity_ledger = {}
    dispatcher._event_source = events
    snapshot = await dispatcher.reconstruct_complexity_from_events(task.id)

    assert snapshot is not None
    assert snapshot["mutation_count"] >= 1
    assert snapshot["execute_observed"] is True
    assert snapshot["opaque_observed"] is True
    assert ComplexityLedger(dispatcher._complexity_ledger).is_complex(task.id) is True


def test_event_reconstruction_ignores_unrelated_events():
    ledger = ComplexityLedger()
    events = [
        make_event("CapabilityCompleted", {"capability_id": "fs"}),
        make_event("TaskStarted", {}),
    ]
    assert ledger.reconstruct_from_events("task", events)["mutation_count"] == 0
    assert ledger.is_complex("task") is False
