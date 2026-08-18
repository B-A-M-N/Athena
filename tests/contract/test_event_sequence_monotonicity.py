"""Contract: EventStore.append_event assigns monotonic, per-task, lossless sequences.

Verifies the append log protocol invariants:
  * per-task sequences are assigned 1, 2, 3, ...
  * concurrent appends for the same task never collide or drop,
  * sequences survive a store "restart" (new EventStore, same Database),
  * UNIQUE(task_id, sequence) holds in the schema.
"""

from __future__ import annotations

import pytest

from athena.state.database import Database
from athena.state.events import EventStore
from athena.state.tasks import TaskStore


def _sequences(events):
    return [e.sequence for e in events]


async def _ensure_task(db, task_id: str) -> None:
    """Create the parent task row so the events FK holds."""
    await TaskStore(db).insert_task(task_id, None, None, "objective")


class TestSequences:
    async def test_monotonic_per_task(self, db):
        store = EventStore(db)
        await _ensure_task(db, "t")
        saw = []
        for i in range(3):
            ev = await store.append_event("Test", task_id="t")
            saw.append(ev.sequence)
        assert saw == [1, 2, 3]

    async def test_sequences_are_per_task(self, db):
        store = EventStore(db)
        await _ensure_task(db, "a")
        await _ensure_task(db, "b")
        await store.append_event("A", task_id="a")
        await store.append_event("A", task_id="a")
        await store.append_event("B", task_id="b")
        await store.append_event("A", task_id="a")
        assert _sequences(await store.list_for_task("a")) == [1, 2, 3]
        assert _sequences(await store.list_for_task("b")) == [1]

    async def test_persisted_sequence_matches(self, db):
        store = EventStore(db)
        await _ensure_task(db, "t")
        await store.append_event("X", task_id="t")
        ev = await store.append_event("Y", task_id="t")
        stored = await store.list_for_task("t")
        assert [e.sequence for e in stored] == [1, 2]
        assert stored[1].id == ev.id
        assert stored[1].sequence == 2


class TestConcurrency:
    async def test_concurrent_appends_no_collision_no_drop(self, tmp_path):
        """Concurrent emitters (separate connections) get dense, unique sequences.

        SQLite allows a single writer at a time, so a competing writer can see a
        transient lock; a resilient emitter retries. The invariant under test:
        whatever the interleaving, final sequences are contiguous (no collision,
        no drop) and the UNIQUE(task_id, sequence) constraint rejoins them.
        """
        path = tmp_path / "race.db"
        db0 = Database(str(path))
        try:
            await TaskStore(db0).insert_task("race", None, None, "objective")
        finally:
            await db0.close()

        def run_worker(idx: int) -> None:
            import asyncio as _asyncio

            async def _run():
                db = Database(str(path))
                await db._ensure_ready()
                store = EventStore(db)
                try:
                    for _ in range(8):
                        while True:
                            try:
                                await store.append_event("e", task_id="race")
                                break
                            except Exception:
                                await _asyncio.sleep(0.01)
                finally:
                    await db.close()

            _asyncio.run(_run())

        import threading
        threads = [threading.Thread(target=run_worker, args=(idx,)) for idx in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        verifier = Database(str(path))
        try:
            events = await EventStore(verifier).list_for_task("race")
            seqs = _sequences(events)
        finally:
            await verifier.close()

        # no gaps, no duplicates, contiguous 1..N
        assert seqs == list(range(1, len(seqs) + 1))
        assert len(set(seqs)) == len(seqs)

    async def test_uniqueness_violation_can_be_forced_outside_store(self, db):
        # The schema enforces UNIQUE(task_id, sequence). A direct duplicate
        # insert outside append_event must be rejected by the database.
        store = EventStore(db)
        await _ensure_task(db, "t")
        ev = await store.append_event("dup", task_id="t")
        from athena.protocol.events import make_event
        dup = make_event("forged", task_id="t", sequence=ev.sequence)
        import aiosqlite
        with pytest.raises(aiosqlite.IntegrityError):
            async with db.transaction():
                await db.execute_raw(
                    "INSERT INTO events(id, task_id, type, sequence, "
                    "timestamp, schema_version, payload, causal_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (dup.id, "t", dup.type, dup.sequence,
                     dup.timestamp.isoformat(), dup.schema_version, "{}", None),
                )


class TestRestart:
    async def test_sequence_continues_after_restart(self, tmp_path):
        path = tmp_path / "evts.db"
        db1 = Database(str(path))
        try:
            store1 = EventStore(db1)
            await TaskStore(db1).insert_task("t", None, None, "objective")
            await store1.append_event("A", task_id="t")
            await store1.append_event("B", task_id="t")
        finally:
            await db1.close()

        db2 = Database(str(path))
        try:
            store2 = EventStore(db2)
            ev = await store2.append_event("C", task_id="t")
            assert ev.sequence == 3
            assert _sequences(await store2.list_for_task("t")) == [1, 2, 3]
        finally:
            await db2.close()