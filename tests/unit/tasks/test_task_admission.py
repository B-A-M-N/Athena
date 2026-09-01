from __future__ import annotations

import pytest

from athena.protocol.errors import ModelProviderUnconfigured
from athena.protocol.tasks import TaskSpec
from athena.state.database import Database
from athena.state.events import EventStore
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore
from athena.tasks.manager import TaskManager


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
