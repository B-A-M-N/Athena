from __future__ import annotations

import pytest

from athena.protocol.errors import ModelProviderUnconfigured
from athena.protocol.tasks import TaskSpec
from athena.state.database import Database
from athena.state.events import EventStore
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore
from athena.tasks.manager import TaskManager


class _BoomBudget:
    """Budget tracker whose registration intentionally fails."""
    def __init__(self):
        self.calls = 0
    def register(self, spec) -> None:
        self.calls += 1
        raise RuntimeError("ledger backend down")


class _BoomCancellations:
    def __init__(self):
        self.calls = 0
    def reset(self, task_id) -> None:
        self.calls += 1
        raise RuntimeError("reset backend down")


class _BoomEvents:
    """Event store whose append_event intentionally fails."""
    def __init__(self):
        self.emitted = 0
    async def append_event(self, *args, **kwargs):
        self.emitted += 1
        raise RuntimeError("event bus down")


@pytest.mark.asyncio
async def test_authority_commits_before_bookkeeping_and_bookkeeping_failure_is_nonfatal() -> None:
    """Durability split (task #11): the durable task row is the authority and
    must commit BEFORE any bookkeeping. A failing budget/cancellation
    registration or event emit must NOT surface as a failed ``create`` for a
    task that was actually admitted, and the row must remain durably present."""
    db = Database(":memory:")
    await db._ensure_ready()
    try:
        sessions = SessionRepository(db)
        tasks = TaskStore(db)
        budgets = _BoomBudget()
        cancellations = _BoomCancellations()
        events = _BoomEvents()

        manager = TaskManager(
            task_store=tasks,
            events=events,
            sessions=sessions,
            budgets=budgets,
            cancellations=cancellations,
        )
        spec = TaskSpec(id="durable-task", objective="must persist", session_id="new-session")

        # create does NOT raise even though every bookkeeping sink fails.
        created = await manager.create(spec)

        assert created.id == spec.id
        # The authority row is durably present regardless of bookkeeping loss.
        assert await tasks.get("durable-task") is not None
        assert await sessions.get("new-session") is not None
        # The bookkeeping was attempted; its failures were logged, not raised.
        assert budgets.calls == 1
        assert events.emitted == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_task_manager_admits_before_creating_session_or_task() -> None:
    db = Database(":memory:")
    await db._ensure_ready()
    try:
        sessions = SessionRepository(db)
        tasks = TaskStore(db)
        admitted = []

        async def reject(spec) -> None:
            admitted.append(spec.id)
            raise ModelProviderUnconfigured("provider unavailable")

        manager = TaskManager(
            task_store=tasks,
            events=EventStore(db),
            sessions=sessions,
            admission=reject,
        )
        spec = TaskSpec(id="admission-task", objective="must not persist", session_id="new-session")

        with pytest.raises(ModelProviderUnconfigured):
            await manager.create(spec)

        assert admitted == ["admission-task"]
        assert await sessions.get("new-session") is None
        assert await tasks.get("admission-task") is None
    finally:
        await db.close()
