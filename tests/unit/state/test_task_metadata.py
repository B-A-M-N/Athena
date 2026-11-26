"""Atomic task metadata mutation (P0/P1).

The read-modify-write of the metadata document must be one database
transaction so concurrent writers cannot overwrite each other's fields, and
each mutator must preserve every unrelated field.
"""

from __future__ import annotations

import asyncio

import pytest

from athena.state.database import Database
from athena.state.tasks import TaskStore


async def _make_store() -> tuple[TaskStore, Database]:
    db = Database(":memory:")
    await db._ensure_ready()
    return TaskStore(db), db


async def _insert(store: TaskStore, task_id: str = "t1") -> None:
    await store.insert_task(task_id, None, None, "objective", metadata={"existing": {"a": 1}})


async def test_metadata_mutators_preserve_other_fields():
    store, db = await _make_store()
    try:
        await _insert(store)
        await store.set_retry_count("t1", 3)
        await store.persist_budget_usage("t1", {"tokens": 42})
        await store.persist_runtime_recovery_hint(
            "t1", runtime_session_id="rs_1", backend="sh", cwd="/work"
        )
        await store.record_recovery_marker("t1", {"reason": "probe"})

        task = await store.get("t1")
        assert task is not None
        meta = task["metadata"]
        assert meta["existing"] == {"a": 1}
        assert meta["worker_retries"] == 3
        assert meta["_budget_usage"] == {"tokens": 42}
        assert meta["_runtime_recovery_hint"]["runtime_session_id"] == "rs_1"
        assert meta["recovery_markers"] == [{"reason": "probe"}]
        assert meta["recovery_required"] is True
    finally:
        await db.close()


async def test_metadata_multiple_recovery_markers_are_appended():
    store, db = await _make_store()
    try:
        await _insert(store)
        for i in range(40):
            await store.record_recovery_marker("t1", {"seq": i})
        task = await store.get("t1")
        assert task is not None
        markers = task["metadata"]["recovery_markers"]
        assert len(markers) == 32
        assert [m["seq"] for m in markers] == list(range(8, 40))
    finally:
        await db.close()


async def test_concurrent_metadata_writers_do_not_lose_fields():
    """Two tasks racing to mutate different metadata fields must both stick."""
    store, db = await _make_store()
    try:
        await _insert(store)
        barrier = asyncio.Barrier(2)

        async def writer_a():
            async def mut():
                await barrier.wait()
                await store.set_retry_count("t1", 7)

            return await mut()

        async def writer_b():
            async def mut():
                await barrier.wait()
                await store.persist_budget_usage("t1", {"tokens": 99})

            return await mut()

        await asyncio.gather(writer_a(), writer_b())
        task = await store.get("t1")
        assert task is not None
        assert task["metadata"]["worker_retries"] == 7
        assert task["metadata"]["_budget_usage"] == {"tokens": 99}
        assert task["metadata"]["existing"] == {"a": 1}
    finally:
        await db.close()


async def test_missing_task_metadata_mutators_are_noops_or_raise():
    store, db = await _make_store()
    try:
        await store.set_retry_count("missing", 1)  # no-op, no raise
        await store.persist_budget_usage("missing", {"tokens": 1})
        await store.persist_runtime_recovery_hint("missing", runtime_session_id="rs_1")
        with pytest.raises(KeyError, match="Task not found"):
            await store.record_recovery_marker("missing", {"reason": "x"})
    finally:
        await db.close()
