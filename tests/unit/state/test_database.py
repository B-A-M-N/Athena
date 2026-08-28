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
