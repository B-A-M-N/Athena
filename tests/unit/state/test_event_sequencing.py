"""Regression tests for P0-20 (centralized DB-backed sequencing) and
P0-21 (persist causal_id)."""

from __future__ import annotations

import asyncio
import time

import pytest

from athena.protocol.events import make_event
from athena.protocol.ids import new_id
from athena.protocol.tasks import ResourceBudget, TaskSpec
from athena.state.database import Database
from athena.state.events import EventStore
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore
from athena.tasks.manager import TaskManager


async def _create_task(events: EventStore) -> TaskSpec:
    db = events._db
    sessions = SessionRepository(db)
    manager = TaskManager(task_store=TaskStore(db), events=events, sessions=sessions)
    session_id = new_id("session")
    await sessions.create(session_id)
    spec = TaskSpec(
        id=new_id("task"),
        objective="x",
        session_id=session_id,
        resource_budget=ResourceBudget(),
    )
    await manager.create(spec)
    await manager.enqueue(spec.id)
    await manager.acquire(spec.id)
    return spec


@pytest.fixture
async def db():
    db = Database(":memory:")
    await db._ensure_ready()
    yield db
    await db.close()


async def test_two_emitters_without_managed_sequence_do_not_collide(db):
    events = EventStore(db)
    task = await _create_task(events)
    # Emitter A and B both write WITHOUT manufacturing a sequence (sequence=None).
    for i in range(4):
        await events.append_event("EmitterA", {"i": i}, task_id=task.id)
        await events.append_event("EmitterB", {"i": i}, task_id=task.id)
    all_events = await events.list_for_task(task.id)
    seqs = [e.sequence for e in all_events]
    assert seqs == list(range(1, len(all_events) + 1))
    assert len(set(seqs)) == len(seqs)


async def test_retrofit_append_assigns_atomic_sequence(db):
    events = EventStore(db)
    task = await _create_task(events)
    # append() (used by dispatcher/skills sinks) must not rely on caller sequence.
    await events.append(make_event("TypeA", {}, task_id=task.id))
    await events.append(make_event("TypeB", {}, task_id=task.id))
    seqs = [e.sequence for e in await events.list_for_task(task.id)]
    assert seqs == list(range(1, len(seqs) + 1))


async def test_sequence_continues_across_restart(db):
    events = EventStore(db)
    task = await _create_task(events)
    for i in range(3):
        await events.append_event("FirstRun", {"i": i}, task_id=task.id)
    max_before = await events.last_sequence(task.id)

    # Simulated restart: brand-new EventStore over the SAME Database file.
    restarted = EventStore(db)
    await restarted.append_event("PostRestart", {}, task_id=task.id)
    seqs = [e.sequence for e in await events.list_for_task(task.id)]
    assert seqs == list(range(1, len(seqs) + 1))
    assert seqs[-1] == max_before + 1


async def test_causal_id_persists_and_reads_back(db):
    events = EventStore(db)
    task = await _create_task(events)
    await events.append_event("CapabilityRequested", {}, task_id=task.id, causal_id="call-0001")
    [event] = [e for e in await events.list_for_task(task.id) if e.type == "CapabilityRequested"]
    assert event.causal_id == "call-0001"


async def test_causal_id_none_by_default(db):
    events = EventStore(db)
    task = await _create_task(events)
    await events.append_event("Plain", {}, task_id=task.id)
    [event] = [e for e in await events.list_for_task(task.id) if e.type == "Plain"]
    assert event.causal_id is None


async def test_same_process_append_wakes_waiter(db):
    events = EventStore(db)
    generation = events.append_generation
    waiter = asyncio.create_task(events.wait_for_append(generation))
    await asyncio.sleep(0)

    await events.append_event("TaskStarted")

    assert await asyncio.wait_for(waiter, timeout=0.25) > generation


async def test_fast_stream_subscriber_is_filtered_and_does_not_backpressure_append(db):
    events = EventStore(db)
    received: list[str] = []

    async def slow(event):
        received.append(event.type)
        await asyncio.sleep(0.01)

    events.subscribe(slow, event_types={"ModelDelta"})
    started = time.monotonic()
    for index in range(500):
        await events.append_event("ModelDelta", {"text": str(index)})
    elapsed = time.monotonic() - started

    # The durable append path must not wait for 500 * 10 ms of presentation
    # work. The bounded queue may coalesce/drop intermediate stream frames.
    assert elapsed < 2.0
    await asyncio.sleep(0.2)
    assert received
    assert all(item == "ModelDelta" for item in received)

    events.unsubscribe(slow)
