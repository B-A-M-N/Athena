import json
from datetime import timedelta

import pytest

from athena.state.database import Database
from athena.memory.embeddings import SemanticRetrievalUnavailable
from athena.memory.store import MemoryStore, memory_content_hash
from athena.protocol.memory import (
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    RetrievalMode,
)
from athena.protocol.messages import Provenance, SourceType, TrustClass, utcnow


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


async def test_same_subject_equal_trust_is_flagged_not_overwritten(store):
    original = MemoryRecord(
        id="mem_pref_a",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope.GLOBAL,
        content="Use dark mode for the operator interface.",
        subject="operator theme",
        trust=TrustClass.USER_CONTENT,
    )
    correction = MemoryRecord(
        id="mem_pref_b",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope.GLOBAL,
        content="Use light mode for the operator interface.",
        subject="operator theme",
        trust=TrustClass.USER_CONTENT,
    )

    await store.save(original)
    saved = await store.save(correction)

    assert saved.id != correction.id
    assert saved.contradicted_by == (original.id,)
    assert len(await store.list_by_kind(MemoryKind.SEMANTIC)) == 2


async def test_project_conflicts_do_not_cross_project_boundaries(store):
    first = MemoryRecord(
        id="mem_project_a",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope.PROJECT,
        content="The deployment branch is main.",
        subject="deployment branch",
        metadata={"scope_id": "project-a"},
        trust=TrustClass.USER_CONTENT,
    )
    second = MemoryRecord(
        id="mem_project_b",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope.PROJECT,
        content="The deployment branch is release.",
        subject="deployment branch",
        metadata={"scope_id": "project-b"},
        trust=TrustClass.USER_CONTENT,
    )

    await store.save(first)
    saved = await store.save(second)

    assert saved.id == second.id
    assert saved.contradicted_by == ()


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
        mode=RetrievalMode.RELEVANCE,
        limit=5,
    )
    assert {r.id for r in by_scope} == {"mem_sem_1"}

    via_search = await store.search(
        "production database",
        limit=5,
        scope=MemoryScope.PROJECT,
        scope_id=None,
        mode=RetrievalMode.RELEVANCE,
        tags=("deploy",),
    )
    assert {r.id for r in via_search} == {"mem_sem_1"}


async def test_normal_retrieval_excludes_inactive_conflicted_and_superseded_records(store):
    now = utcnow()
    records = [
        MemoryRecord(
            id="mem-current-policy",
            kind=MemoryKind.SEMANTIC,
            scope=MemoryScope.PROJECT,
            content="current release policy uses signed receipts",
            metadata={"scope_id": "retrieval-current"},
        ),
        MemoryRecord(
            id="mem-expired-policy",
            kind=MemoryKind.SEMANTIC,
            scope=MemoryScope.PROJECT,
            content="expired release policy uses unsigned receipts",
            valid_until=now - timedelta(minutes=1),
            metadata={"scope_id": "retrieval-expired"},
        ),
        MemoryRecord(
            id="mem-future-policy",
            kind=MemoryKind.SEMANTIC,
            scope=MemoryScope.PROJECT,
            content="future release policy uses experimental receipts",
            valid_from=now + timedelta(minutes=1),
            metadata={"scope_id": "retrieval-future"},
        ),
        MemoryRecord(
            id="mem-conflicted-policy",
            kind=MemoryKind.SEMANTIC,
            scope=MemoryScope.PROJECT,
            content="conflicted release policy uses unsigned receipts",
            contradicted_by=("mem-current-policy",),
            metadata={"scope_id": "retrieval-conflicted"},
        ),
        MemoryRecord(
            id="mem-old-policy",
            kind=MemoryKind.SEMANTIC,
            scope=MemoryScope.PROJECT,
            content="superseded release policy uses unsigned receipts",
            metadata={"scope_id": "retrieval-old"},
        ),
        MemoryRecord(
            id="mem-new-policy",
            kind=MemoryKind.SEMANTIC,
            scope=MemoryScope.PROJECT,
            content="successor release policy uses signed receipts",
            supersedes=("mem-old-policy",),
            metadata={"scope_id": "retrieval-new"},
        ),
    ]
    for record in records:
        await store.save(record)

    current = await store.search("release policy receipts", scope=MemoryScope.PROJECT, limit=20)
    assert {record.id for record in current} == {"mem-current-policy", "mem-new-policy"}

    history = await store.search(
        "release policy receipts",
        scope=MemoryScope.PROJECT,
        mode=RetrievalMode.HISTORY,
        limit=20,
    )
    assert {record.id for record in history} == {record.id for record in records}

    explicit = await store.search(
        "release policy receipts",
        scope=MemoryScope.PROJECT,
        limit=20,
        include_inactive=True,
        include_conflicts=True,
    )
    assert {record.id for record in explicit} == {record.id for record in records}


class _EmbeddingProvider:
    model = "test-embedding"
    version = "v1"

    async def embed(self, text: str) -> tuple[float, float]:
        text = text.casefold()
        return (1.0, 0.0) if any(word in text for word in ("brief", "verbose")) else (0.0, 1.0)


async def test_semantic_mode_requires_explicit_embedding_provider(store):
    with pytest.raises(SemanticRetrievalUnavailable, match="no MemoryEmbeddingProvider"):
        await store.search(
            "How verbose should I be?",
            scope=MemoryScope.USER,
            scope_id="alice",
            mode=RetrievalMode.SEMANTIC,
        )


async def test_semantic_retrieval_uses_persisted_embedding_for_paraphrase():
    db = Database(":memory:")
    embedding_store = MemoryStore(db, embedding_provider=_EmbeddingProvider())
    record = MemoryRecord(
        id="mem-paraphrase",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope.USER,
        content="The operator prefers brief technical responses.",
        metadata={"scope_id": "alice"},
        confidence=0.95,
    )
    await embedding_store.save(record)

    found = await embedding_store.search(
        "How verbose should I be?",
        scope=MemoryScope.USER,
        scope_id="alice",
        mode=RetrievalMode.SEMANTIC,
        limit=5,
    )
    row = await db.fetch_one(
        "SELECT content_hash, embedding_model, embedding_version FROM memory_embeddings "
        "WHERE memory_id = ?",
        (record.id,),
    )

    assert [item.id for item in found] == [record.id]
    assert found[0].metadata["_athena:retrieval_score"] == pytest.approx(1.0)
    assert found[0].metadata["_athena:vector_score"] == pytest.approx(1.0)
    assert row["content_hash"] == memory_content_hash(record)
    assert row["embedding_model"] == "test-embedding"
    assert row["embedding_version"] == "v1"
    await db.close()


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


async def test_promoted_candidate_preserves_canonical_metadata_and_scope_retrieval(store):
    pending = MemoryRecord(
        id="mem_promote_canonical",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope.TASK,
        content="The operator prefers concise release notes.",
        summary="release-note preference",
        source=Provenance(
            source_type=SourceType.USER,
            source_id="task-1",
            trust=TrustClass.USER_CONTENT,
            scope="task:task-1",
        ),
        trust=TrustClass.USER_CONTENT,
        metadata={
            "pending_promotion": True,
            "candidate_type": "explicit_user_fact",
            "scope_id": "task-1",
            "custom": "retained",
        },
        retrieval_mode=RetrievalMode.RELEVANCE,
        subject="release notes",
        tags=("release", "preference"),
        source_refs=("msg-1",),
        confidence=0.91,
        supersedes=("mem-old",),
        contradicted_by=("mem-conflict",),
    )
    await store.save(pending)

    promoted = await store.promote_pending_candidate(
        pending.id,
        scope=MemoryScope.USER,
        scope_id="principal-1",
    )

    assert promoted is not None
    assert promoted.scope is MemoryScope.USER
    assert promoted.trust is TrustClass.USER_CONTENT
    assert promoted.source is not None and promoted.source.source_id == "task-1"
    assert promoted.metadata["custom"] == "retained"
    assert promoted.metadata["pending_promotion"] is False
    assert promoted.metadata["promotion"] == "promoted"
    assert promoted.retrieval_mode is RetrievalMode.RELEVANCE
    assert promoted.tags == ("release", "preference")
    assert promoted.confidence == 0.91
    assert promoted.supersedes == ("mem-old",)
    assert promoted.contradicted_by == ("mem-conflict",)
    assert [
        item.id
        for item in await store.search(
            "concise release notes",
            scope=MemoryScope.USER,
            scope_id="principal-1",
            include_conflicts=True,
        )
    ] == [pending.id]

    raw = await store._db.fetch_one("SELECT metadata FROM memories WHERE id = ?", (pending.id,))
    metadata = json.loads(raw["metadata"])
    assert metadata["_athena:scope_id"] == "principal-1"
    assert metadata["_athena:provenance"]["source_id"] == "task-1"
