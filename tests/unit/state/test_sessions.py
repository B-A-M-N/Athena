import pytest

from athena.protocol.tasks import TaskStatus
from athena.state.database import Database
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore


@pytest.fixture
async def db():
    db = Database(":memory:")
    yield db
    await db.close()


@pytest.fixture
async def repo(db):
    repo = SessionRepository(db)
    await repo.create("sess_1")
    return repo


async def test_create_and_get_session(db):
    repo = SessionRepository(db)
    await repo.create("sess_1", metadata={"foo": "bar"})
    row = await repo.get("sess_1")
    assert row is not None
    assert row["id"] == "sess_1"
    assert row["metadata"] == {"foo": "bar"}
    assert await repo.get("nope") is None


async def test_task_transition_legal(repo, db):
    store = TaskStore(db)
    await store.insert_task("task_1", "sess_1", None, "do a thing")
    await store.transition("task_1", TaskStatus.QUEUED)
    row = await store.get("task_1")
    assert row["status"] == TaskStatus.QUEUED.value


async def test_task_transition_illegal_rejected(repo, db):
    store = TaskStore(db)
    await store.insert_task("task_2", "sess_1", None, "do another thing")
    for status in (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.COMPLETE):
        await store.transition("task_2", status)
    with pytest.raises(ValueError):
        await store.transition("task_2", TaskStatus.RUNNING)
    row = await store.get("task_2")
    assert row["status"] == TaskStatus.COMPLETE.value


