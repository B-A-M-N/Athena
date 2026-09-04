from datetime import timedelta

import pytest

from athena.state.database import Database
from athena.memory.store import MemoryStore
from athena.protocol.memory import (
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    RetrievalMode,
)
from athena.protocol.messages import TrustClass, utcnow


@pytest.fixture
async def store():
    db = Database(":memory:")
    yield MemoryStore(db)
    await db.close()


async def test_save_and_get_round_trips_by_id(store):
    record = MemoryRecord(
        id="mem_1",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope.PROJECT,
        content="The cache is invalidated on every write.",
        summary="cache invalidation rule",
    )
    await store.save(record)

    got = await store.get("mem_1")
    assert got is not None
    assert got.id == "mem_1"
    assert got.kind is MemoryKind.SEMANTIC
    assert got.scope is MemoryScope.PROJECT
    assert got.content == record.content
    assert got.summary == "cache invalidation rule"


async def test_lower_trust_same_id_rejected(store):
    higher = MemoryRecord(
        id="mem_z",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope.PROJECT,
        content="The API base URL is https://api.example.com",
        trust=TrustClass.AUTHORITY,
    )
    lower = MemoryRecord(
        id="mem_z",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope.PROJECT,
        content="The API base URL is https://api.example.com (revised)",
        trust=TrustClass.UNTRUSTED,
    )
    await store.save(higher)
    await store.save(lower)

    got = await store.get("mem_z")
    assert got is not None
    assert got.trust is TrustClass.AUTHORITY
    assert got.content == higher.content


async def test_recall_and_search_filter_by_tags_and_scope(store):
    project = MemoryRecord(
        id="mem_sem_1",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope.PROJECT,
        content="The production database uses standard replication",
        tags=("deploy", "prod"),
    )
    session = MemoryRecord(
        id="mem_sem_2",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope.SESSION,
        content="The production database uses standard replication config",
        tags=("deploy", "test"),
    )
    await store.save(project)
    await store.save(session)

    by_scope = await store.recall(
        "production database",
        tags=("deploy",),
        scope=MemoryScope.PROJECT,
        scope_id=None,
        mode=RetrievalMode.SEMANTIC,
        limit=5,
    )
    assert {r.id for r in by_scope} == {"mem_sem_1"}

    via_search = await store.search(
        "production database",
        limit=5,
        scope=MemoryScope.PROJECT,
        scope_id=None,
        mode=RetrievalMode.SEMANTIC,
        tags=("deploy",),
    )
    assert {r.id for r in via_search} == {"mem_sem_1"}


async def test_record_round_trips_section62_fields(store):
    valid_from = utcnow()
    record = MemoryRecord(
        id="mem_62",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope.PROJECT,
        content="prefer async I/O for network calls in hot paths",
        summary="concurrency guidance",
        trust=TrustClass.AGENT_CURATED,
        subject="network io",
        tags=("async", "io"),
        source_refs=("art://a",),
        confidence=0.85,
        valid_from=valid_from,
        supersedes=("mem_old",),
        contradicted_by=("mem_con",),
    )
    await store.save(record)

    got = await store.get("mem_62")
    assert got.subject == "network io"
    assert got.tags == ("async", "io")
    assert got.source_refs == ("art://a",)
    assert got.confidence == 0.85
    assert got.supersedes == ("mem_old",)
    assert got.contradicted_by == ("mem_con",)
    assert got.valid_from == valid_from


async def test_pending_candidate_has_promote_discard_and_expire_lifecycle(store):
    pending = MemoryRecord(
        id="mem_pending",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope.TASK,
        content="the project appears to use a bounded retry budget",
        metadata={"pending_promotion": True, "task_id": "task-1"},
    )
    await store.save(pending)

    assert [item.id for item in await store.list_pending_candidates()] == ["mem_pending"]
    promoted = await store.promote_pending_candidate(
        "mem_pending",
        scope=MemoryScope.SESSION,
        scope_id="session-1",
    )
    assert promoted is not None
    assert promoted.scope is MemoryScope.SESSION
    assert promoted.metadata["pending_promotion"] is False
    assert await store.list_pending_candidates() == []

    discarded = MemoryRecord(
        id="mem_discard",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope.TASK,
        content="discard this inferred lesson",
        metadata={"pending_promotion": True},
    )
    await store.save(discarded)
    assert await store.discard_pending_candidate("mem_discard") is True
    assert await store.get("mem_discard") is None

    expiring = MemoryRecord(
        id="mem_expire",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope.TASK,
        content="old inferred lesson",
        metadata={"pending_promotion": True},
    )
    await store.save(expiring)
    await store._db.execute(
        "UPDATE memories SET created_at = ? WHERE id = ?",
        ((utcnow() - timedelta(days=31)).isoformat(), "mem_expire"),
    )
    assert await store.expire_pending_candidates(utcnow() - timedelta(days=30)) == 1
    assert await store.get("mem_expire") is None
