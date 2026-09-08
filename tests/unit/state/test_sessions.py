import pytest

from athena.protocol.messages import Message, Provenance, Role, SourceType, TextBlock, utcnow
from athena.protocol.tasks import TaskStatus
from athena.state.database import Database
from athena.state.messages import MessageStore
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


async def test_list_all_sessions(db):
    repo = SessionRepository(db)
    await repo.create("sess_1", metadata={"foo": "bar"})
    await repo.create("sess_2")

    rows = await repo.list_all()

    assert [row["id"] for row in rows] == ["sess_1", "sess_2"]
    assert rows[0]["metadata"] == {"foo": "bar"}


async def test_close_marks_session_without_deleting_transcript(repo):
    assert await repo.close("sess_1") is True
    row = await repo.get("sess_1")
    assert row is not None
    assert row["metadata"]["state"] == "closed"
    assert "closed_at" in row["metadata"]
    assert await repo.close("sess_1") is True
    assert await repo.close("missing") is False


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


async def test_recent_messages_returns_newest_tail_in_chronological_order(repo):
    for index in range(150):
        await repo.append_message(
            Message(
                id=f"message-{index:03d}",
                role=Role.USER,
                blocks=(TextBlock(text=str(index)),),
                created_at=utcnow(),
                provenance=Provenance(source_type=SourceType.USER),
            )
        )

    recent = await repo.list_recent_messages("sess_1", limit=100)

    assert [message.id for message in recent] == [
        f"message-{index:03d}" for index in range(50, 150)
    ]


async def test_canonical_user_turn_is_idempotent_by_stable_message_identity(db):
    sessions = SessionRepository(db)
    await sessions.create("sess-canonical")
    messages = MessageStore(db)
    message = Message(
        id="msg_user_task-canonical",
        role=Role.USER,
        blocks=(TextBlock(text="do the task"),),
        created_at=utcnow(),
        provenance=Provenance(source_type=SourceType.USER),
        metadata={
            "session_id": "sess-canonical",
            "task_id": "task-canonical",
            "canonical_user_turn": True,
        },
    )

    assert await messages.append_user_turn("sess-canonical", message) is True
    assert await messages.append_user_turn("sess-canonical", message) is False
    assert await messages.count_session_messages("sess-canonical") == 1
