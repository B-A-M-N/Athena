import asyncio
import time

import pytest

from athena.state.database import Database


@pytest.fixture
async def db():
    db = Database(":memory:")
    yield db
    await db.close()


async def test_in_memory_connect_and_migrations_run(db):
    await db._ensure_ready()
    rows = await db.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in rows}
    assert "sessions" in names
    assert "tasks" in names
    assert "messages" in names
    assert "schema_migrations" in names
    assert "workflow_step_item_runs" in names


async def test_worker_completion_wakes_the_default_event_loop(db):
    await asyncio.wait_for(db._ensure_ready(), timeout=0.5)
    await asyncio.wait_for(db.close(), timeout=0.5)


async def test_transaction_commits_on_success(db):
    await db._ensure_ready()
    async with db.transaction():
        await db.execute_raw(
            "INSERT INTO sessions(id, parent_id, created_at, updated_at, metadata) VALUES (?, ?, ?, ?, ?)",
            ("s_1", None, "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00", "{}"),
        )
    row = await db.fetch_one("SELECT id FROM sessions WHERE id = 's_1'")
    assert row is not None


async def test_transaction_rolls_back_on_exception(db):
    await db._ensure_ready()
    with pytest.raises(RuntimeError):
        async with db.transaction():
            await db.execute_raw(
                "INSERT INTO sessions(id, parent_id, created_at, updated_at, metadata) VALUES (?, ?, ?, ?, ?)",
                ("s_2", None, "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00", "{}"),
            )
            raise RuntimeError("boom")
    row = await db.fetch_one("SELECT id FROM sessions WHERE id = 's_2'")
    assert row is None


async def test_slow_sqlite_work_does_not_stall_asyncio_heartbeat(db):
    await db._ensure_ready()
    connection = db._conn
    assert connection is not None
    await connection._call(  # noqa: SLF001 - install a deterministic test-only SQLite function
        lambda: connection._require_connection().create_function("pause", 1, time.sleep)  # noqa: SLF001
    )

    heartbeat = 0

    async def beat() -> None:
        nonlocal heartbeat
        for _ in range(10):
            heartbeat += 1
            await asyncio.sleep(0.01)

    beat_task = asyncio.create_task(beat())
    await db.fetch_one("SELECT pause(?) AS paused", (0.08,))
    await beat_task

    assert heartbeat >= 5
