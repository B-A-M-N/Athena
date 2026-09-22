"""Regression tests for P0-20 (centralized DB-backed sequencing) and
P0-21 (persist causal_id)."""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlite3 import IntegrityError

from athena.protocol.events import make_event
from athena.protocol.ids import new_id
from athena.protocol.tasks import ResourceBudget, TaskSpec
from athena.state.database import Database
from athena.state.events import EventIdConflictError, EventStore
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


async def test_fast_events_flush_before_task_control_event(db):
    events = EventStore(db)
    task = await _create_task(events)

    await events.append_event("ModelDelta", {"text": "partial"}, task_id=task.id)
    await events.append_event("TaskCompleted", {"status": "complete"}, task_id=task.id)

    stored = await events.list_for_task(task.id)
    assert [event.type for event in stored][-2:] == ["ModelDelta", "TaskCompleted"]
    assert [event.sequence for event in stored] == list(range(1, len(stored) + 1))


async def test_fast_events_batch_without_control_event_is_replayed_after_flush(db):
    events = EventStore(db)
    task = await _create_task(events)

    for index in range(256):
        await events.append_event("StdoutChunk", {"data": str(index)}, task_id=task.id)

    # Reads are an explicit durability boundary and must return the complete
    # ordered log even when no control event follows the stream.
    stored = await events.list_for_task(task.id)
    chunks = [event for event in stored if event.type == "StdoutChunk"]
    assert len(chunks) == 256
    assert [event.sequence for event in stored] == list(range(1, len(stored) + 1))


async def test_duplicate_fast_event_id_is_idempotent_after_flush(db):
    """A conflicting duplicate fast id must be rejected, not retried or silently
    overwritten (fast events skip append()'s normal precheck)."""
    events = EventStore(db)
    task = await _create_task(events)

    first = make_event("ModelDelta", {"text": "a"}, id="dup-fast", task_id=task.id)
    await events.append(first)
    await events.flush_fast_events()

    duplicate = make_event("ModelDelta", {"text": "b"}, id="dup-fast", task_id=task.id)
    await events.append(duplicate)
    # Stable identity now rejects payload mutation after a redelivered ID.
    with pytest.raises(EventIdConflictError, match="conflicting event identity"):
        await asyncio.wait_for(events.flush_fast_events(), timeout=2.0)

    stored = await events.list_for_task(task.id)
    fast = [e for e in stored if e.id == "dup-fast"]
    assert len(fast) == 1
    assert fast[0].payload == {"text": "a"}
    assert [e.sequence for e in stored] == list(range(1, len(stored) + 1))


async def test_conflicting_fast_ids_within_one_batch_are_rejected(db):
    events = EventStore(db)
    task = await _create_task(events)

    first = make_event("StdoutChunk", {"data": "1"}, id="dup-batch", task_id=task.id)
    different = make_event("StdoutChunk", {"data": "2"}, id="dup-batch", task_id=task.id)
    third = make_event("StdoutChunk", {"data": "3"}, id="fresh-batch", task_id=task.id)
    await events.append(first)
    await events.append(different)
    await events.append(third)
    # Same-ID different-payload is a conflict, even inside one queued batch.
    with pytest.raises(EventIdConflictError, match="conflicting event identity"):
        await asyncio.wait_for(events.flush_fast_events(), timeout=2.0)

    stored = [
        e
        for e in await events.list_for_task(task.id)
        if e.type == "StdoutChunk" and e.id in {"dup-batch", "fresh-batch"}
    ]
    assert [e.id for e in stored] == []


async def test_close_cancels_and_clears_subscriber_workers(db):
    """EventStore.close() must own all subscriber workers, not leave them
    blocked forever on queue.get()."""
    events = EventStore(db)
    received: list[str] = []

    async def sink(event):
        received.append(event.type)

    events.subscribe(sink, event_types={"ModelDelta"})
    await events.append_event("ModelDelta", {"text": "x"})
    await asyncio.sleep(0.05)
    assert received

    subscription = events._subscribers[0]
    assert subscription.worker is not None and not subscription.worker.done()

    await events.close()
    assert not events._subscribers
    assert subscription.worker.cancelled() or subscription.worker.done()


async def test_event_sequence_uses_immediate_transaction_mode(db):
    """Event sequence allocation must own the write lock from BEGIN."""
    # Verify the implementation uses IMMEDIATE (not DEFERRED) so two writers
    # cannot both read the same MAX(sequence) before either commits.
    import inspect

    from athena.state import events as events_module

    source = inspect.getsource(events_module.EventStore._write_batch)
    assert 'transaction(mode="IMMEDIATE")' in source
    assert "transaction() as" not in source


async def test_event_sequence_conflict_is_bounded_not_infinite(db, monkeypatch):
    """Residual sequence collision raises typed error instead of looping."""
    store = EventStore(db)
    task = await _create_task(store)
    await store.append_event("TestEvent", {"n": 1}, task_id=task.id)

    # Simulate that every write hits a sequence collision by making the
    # INSERT always raise IntegrityError with the unique-constraint message.
    from athena.state.events import EventSequenceConflictError

    original_executemany_raw = store._db.executemany_raw

    async def conflicting_executemany_raw(sql, params):
        if "INSERT INTO events" in sql:
            raise IntegrityError("UNIQUE constraint failed: events.task_id, events.sequence")
        return await original_executemany_raw(sql, params)

    monkeypatch.setattr(store._db, "executemany_raw", conflicting_executemany_raw)

    with pytest.raises(EventSequenceConflictError, match="conflicted 8 times"):
        await store.append_event("TestEvent", {"n": 2}, task_id=task.id)


async def test_duplicate_stable_final_event_after_restart_is_idempotent(db):
    """A re-driven final lifecycle event deduplicates instead of failing."""
    store = EventStore(db)
    task = await _create_task(store)
    stable_id = f"task-lifecycle:{task.id}:COMPLETE"

    first = await store.append_event(
        "TaskCompleted",
        {"status": "COMPLETE"},
        task_id=task.id,
        session_id=task.session_id,
        id=stable_id,
    )
    # A restart creates a fresh EventStore and re-emit uses the same identity.
    replayed_store = EventStore(db)
    second = await replayed_store.append_event(
        "TaskCompleted",
        {"status": "COMPLETE"},
        task_id=task.id,
        session_id=task.session_id,
        id=stable_id,
    )

    assert second.id == first.id
    stored = await replayed_store.list_for_task(task.id)
    assert [event.id for event in stored].count(stable_id) == 1
    assert stored[-1].sequence > 0


async def test_identical_stable_id_redelivery_is_idempotent(db):
    """An exact stable-ID replay is idempotent, even if it reaches the race
    fallback rather than the transactional identity precheck."""
    store = EventStore(db)
    task = await _create_task(store)
    stable_id = "race-idempotent"

    await store.append_event("TestEvent", {"value": 1}, task_id=task.id, id=stable_id)
    # Direct _write_batch covers the rare concurrent race fallback.
    replay = make_event("TestEvent", {"value": 1}, task_id=task.id, id=stable_id)
    result = await asyncio.wait_for(store._write_batch((replay,)), timeout=2.0)
    assert result == []  # deduplicated by identical-redelivery handler


async def test_conflicting_stable_id_reuse_raises_typed_error(db, monkeypatch):
    """Same ID with different payload must raise EventIdConflictError when
    the write reaches the database (e.g. racing a concurrent writer)."""
    from athena.state.events import EventIdConflictError

    store = EventStore(db)
    task = await _create_task(store)
    await store.append_event("TestEvent", {"value": 1}, task_id=task.id, id="conflict-id")
    # Simulate the race fallback: the precheck misses the newly committed row
    # and the INSERT reaches the UNIQUE constraint.
    original_fetch = store._db.fetch_all_raw

    async def blind_fetch(sql, *a, **kw):
        if "SELECT id FROM events WHERE id IN" in sql:
            return []
        return await original_fetch(sql, *a, **kw)

    monkeypatch.setattr(store._db, "fetch_all_raw", blind_fetch)
    different = make_event("TestEvent", {"value": 2}, task_id=task.id, id="conflict-id")
    with pytest.raises(EventIdConflictError, match="conflicting"):
        await store._write_batch((different,))
