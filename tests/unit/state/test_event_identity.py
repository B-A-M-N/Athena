"""Stable event IDs require canonical identity, not payload-only equality."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from athena.protocol.events import Event
from athena.protocol.ids import new_id
from athena.protocol.tasks import ResourceBudget, TaskSpec
from athena.state.database import Database
from athena.state.events import EventIdConflictError, EventStore
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore
from athena.tasks.manager import TaskManager


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database._ensure_ready()
    yield database
    await database.close()


@pytest.fixture
def store(db):
    return EventStore(db)


async def _create_task(store: EventStore) -> TaskSpec:
    db = store._db
    sessions = SessionRepository(db)
    manager = TaskManager(task_store=TaskStore(db), events=store, sessions=sessions)
    session_id = new_id("session")
    await sessions.create(session_id)
    spec = TaskSpec(
        id=new_id("task"),
        objective="identity replay",
        session_id=session_id,
        resource_budget=ResourceBudget(),
    )
    await manager.create(spec)
    await manager.enqueue(spec.id)
    await manager.acquire(spec.id)
    return spec


def _identity(
    task: TaskSpec,
    *,
    type_: str = "TestEvent",
    payload: dict | None = None,
    schema_version: int = 1,
):
    return {
        "type": type_,
        "payload": {"value": 1} if payload is None else payload,
        "task_id": task.id,
        "session_id": task.session_id,
        "causal_id": "cause-id",
        "schema_version": schema_version,
    }


def _event(identity, *, id_: str = "stable-id") -> Event:
    now = datetime.now(timezone.utc)
    return Event(
        id=id_,
        type=identity["type"],
        sequence=0,
        timestamp=now,
        task_id=identity["task_id"],
        session_id=identity["session_id"],
        schema_version=identity["schema_version"],
        payload=identity["payload"],
        causal_id=identity["causal_id"],
    )


async def _append(store: EventStore, task: TaskSpec):
    return await store.append_event(
        "TestEvent",
        {"value": 1},
        task_id=task.id,
        session_id=task.session_id,
        causal_id="cause-id",
        id="stable-id",
    )


async def test_exact_redelivery_is_idempotent(store):
    task = await _create_task(store)
    first = await _append(store, task)
    replay = _event(_identity(task))
    result = await store._write_batch((replay,))

    assert result == []
    rows = await store.list_for_task(task.id)
    assert [event.id for event in rows].count("stable-id") == 1
    assert first.sequence > 0


async def test_same_batch_duplicate_is_idempotent(store):
    task = await _create_task(store)
    event = _event(_identity(task))
    result = await store._write_batch((event, event))

    assert [item.id for item in result] == ["stable-id"]
    rows = await store.list_for_task(task.id)
    assert [row.id for row in rows].count("stable-id") == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", "OtherEvent"),
        ("payload", {"value": 2}),
        ("task_id", "other-task"),
        ("session_id", "other-session"),
        ("causal_id", "other-cause"),
        ("schema_version", 2),
    ],
)
async def test_durable_identity_conflict_raises_typed_error(store, field, value):
    task = await _create_task(store)
    await _append(store, task)
    replay_identity = _identity(task)
    replay_identity[field] = value
    replay = _event(replay_identity)

    with pytest.raises(EventIdConflictError, match="conflicting event identity"):
        await store._write_batch((replay,))

    rows = await store.list_for_task(task.id)
    assert [event.id for event in rows].count("stable-id") == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", "OtherEvent"),
        ("payload", {"value": 2}),
        ("task_id", "other-task"),
        ("session_id", "other-session"),
        ("causal_id", "other-cause"),
        ("schema_version", 2),
    ],
)
async def test_in_batch_identity_conflict_raises_before_sequence_allocation(store, field, value):
    task = await _create_task(store)
    left_identity = _identity(task)
    left = _event(left_identity)
    right_identity = _identity(task)
    right_identity[field] = value
    right = _event(right_identity)

    before = await store.list_for_task(task.id)
    with pytest.raises(EventIdConflictError, match="duplicate event id"):
        await store._write_batch((left, right))

    rows = await store.list_for_task(task.id)
    assert rows == before
    assert all(event.id != "stable-id" for event in rows)
