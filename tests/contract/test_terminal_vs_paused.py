"""Contract: final vs paused task lifecycle.

A paused task (e.g. INTERRUPTED) is alive — wait_for must keep polling and
completed_at must be None. A terminal task (COMPLETE / CANCELLED) returns
from wait_for and has completed_at set.
"""

from __future__ import annotations

import asyncio
import pytest

from athena.protocol.tasks import FINAL_STATUSES, TaskStatus
from athena.state.tasks import TaskStore

async def _wait_for(store: TaskStore, task_id: str, *, polls: int = 5, step: float = 0.01):
    """Poll until the task is terminal; returns final status (or None if still waiting)."""
    seen = None
    for _ in range(polls):
        row = await store.get(task_id)
        seen = TaskStatus(row["status"])
        if seen in FINAL_STATUSES:
            return seen
        await asyncio.sleep(step)
    return None

@pytest.mark.athena_claim("BHV-023", "BHV-024", "BHV-022")
@pytest.mark.athena_evidence("test", "invariant")
class TestTerminalVsPaused:
    async def test_interrupted_keeps_waiting(self, db):
        store = TaskStore(db)
        await store.insert_task("t-int", None, None, "x", status=TaskStatus.RUNNING)
        await store.transition("t-int", TaskStatus.INTERRUPTED)
        # Not terminal within the poll window -> wait_for does not return.
        assert await _wait_for(store, "t-int") is None

    async def test_complete_returns(self, db):
        store = TaskStore(db)
        await store.insert_task("t-done", None, None, "x", status=TaskStatus.RUNNING)
        await store.transition("t-done", TaskStatus.COMPLETE)
        assert await _wait_for(store, "t-done") == TaskStatus.COMPLETE

    async def test_interrupted_completed_at_is_none(self, db):
        store = TaskStore(db)
        await store.insert_task("t-i2", None, None, "x", status=TaskStatus.RUNNING)
        await store.transition("t-i2", TaskStatus.INTERRUPTED)
        row = await store.get("t-i2")
        assert row.get("completed_at") is None

    async def test_cancelled_completed_at_is_set(self, db):
        store = TaskStore(db)
        await store.insert_task("t-c2", None, None, "x", status=TaskStatus.RUNNING)
        await store.transition("t-c2", TaskStatus.CANCELLED)
        row = await store.get("t-c2")
        assert row.get("completed_at") is not None
        assert await _wait_for(store, "t-c2") == TaskStatus.CANCELLED