import pytest

from athena.state.database import Database
from athena.memory.store import MemoryStore
from athena.memory.conflicts import (
    ConflictResolution,
    MemoryConflictResolver,
)
from athena.protocol.memory import MemoryKind, MemoryRecord, MemoryScope
from athena.protocol.messages import TrustClass


@pytest.fixture
async def conflict_resolver():
    db = Database(":memory:")
    store = MemoryStore(db)
    yield MemoryConflictResolver(store), store
    await db.close()


def _record(id, content, trust, scope=MemoryScope.PROJECT):
    return MemoryRecord(
        id=id,
        kind=MemoryKind.SEMANTIC,
        scope=scope,
        content=content,
        trust=trust,
    )


async def test_detect_conflict_finds_contradiction(conflict_resolver):
    resolver, store = conflict_resolver
    await store.save(
        _record(
            "s1",
            "the authentication token expires after one hour",
            TrustClass.AGENT_CURATED,
        )
    )
    candidate = _record(
        "s2",
        "the authentication token expires after one hour, not one day",
        TrustClass.AGENT_CURATED,
    )
    report = await resolver.detect_conflict(candidate)
    assert report.conflicting
    assert {c.id for c in report.conflicting} == {"s1"}
    assert report.reason


async def test_resolve_lower_trust_rejects_equal_trust_flags(conflict_resolver):
    resolver, store = conflict_resolver
    await store.save(
        _record(
            "t1",
            "the retry backoff is capped at thirty seconds",
            TrustClass.AUTHORITY,
        )
    )

    lower = _record(
        "t2",
        "the retry backoff is capped at thirty seconds but lower",
        TrustClass.UNTRUSTED,
    )
    lower_report = await resolver.detect_conflict(lower)
    lower_result = await resolver.resolve(lower, lower_report)
    assert lower_result.resolution is ConflictResolution.REJECT

    equal = _record(
        "t2",
        "the retry backoff is capped at thirty seconds and revised",
        TrustClass.AUTHORITY,
    )
    equal_report = await resolver.detect_conflict(equal)
    equal_result = await resolver.resolve(equal, equal_report)
    assert equal_result.resolution is ConflictResolution.FLAG
    assert {c.id for c in equal_result.superseded} == {"t1"}
