"""Conflict-aware durable memory write mechanics."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from athena.memory.conflicts import ConflictResolution, MemoryConflictResolver
from athena.memory.record_codec import record_metadata
from athena.protocol.memory import MemoryKind, MemoryRecord
from athena.protocol.messages import Provenance, SourceType, TrustClass, utcnow

__all__ = ["MemoryWriteCoordinator", "MemoryWriteResult", "merge_links"]


@dataclass(frozen=True)
class MemoryWriteResult:
    """Explicit durable outcome of a memory write attempt."""

    status: str
    memory_id: str | None = None
    record: MemoryRecord | None = None
    reason: str | None = None


_TRUST_RANK: dict[TrustClass, int] = {
    TrustClass.AUTHORITY: 5,
    TrustClass.CONFIGURED_INSTRUCTION: 4,
    TrustClass.USER_CONTENT: 3,
    TrustClass.AGENT_CURATED: 2,
    TrustClass.EXTERNAL_CONTENT: 1,
    TrustClass.UNTRUSTED: 0,
}


def _trust_rank(trust: TrustClass | None) -> int:
    return _TRUST_RANK.get(trust or TrustClass.AGENT_CURATED, 0)


def merge_links(current: Sequence[str], additional: Sequence[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for value in (*current, *additional):
        if value and value not in seen:
            seen.append(value)
    return tuple(seen)


def _replace(record: MemoryRecord, **kwargs: Any) -> MemoryRecord:
    return replace(record, **kwargs)


class MemoryWriteCoordinator:
    """Apply trust/conflict rules and persist one canonical memory row."""

    def __init__(
        self,
        db: Any,
        *,
        list_by_kind: Callable[[MemoryKind], Awaitable[list[MemoryRecord]]],
        get_record: Callable[[str], Awaitable[MemoryRecord | None]],
        merge_superseded: Callable[[Sequence[str], str], Awaitable[None]],
        index_embedding: Callable[[MemoryRecord], Awaitable[bool]],
        eager_index: Callable[[], bool],
        mark_mutation: Callable[[], None],
    ) -> None:
        self._db = db
        self._list_by_kind = list_by_kind
        self._get_record = get_record
        self._merge_superseded = merge_superseded
        self._index_embedding = index_embedding
        self._eager_index = eager_index
        self._mark_mutation = mark_mutation

    async def save(self, record: MemoryRecord) -> MemoryWriteResult:
        trust = record.trust
        if record.source is not None:
            trust = record.source.trust
        provenance = record.source or Provenance(source_type=SourceType.RUNTIME, trust=trust)
        now = utcnow().isoformat()

        resolver = MemoryConflictResolver(self._list_by_kind)
        report = await resolver.detect_conflict(record)

        existing_same = await self._get_record(record.id)
        if existing_same is not None:
            existing_rank = _trust_rank(existing_same.trust)
            incoming_rank = _trust_rank(record.trust)
            if incoming_rank < existing_rank:
                return MemoryWriteResult(
                    status="REJECTED",
                    memory_id=existing_same.id,
                    record=existing_same,
                    reason="incoming memory has lower trust than the existing record",
                )
            if incoming_rank == existing_rank:
                record = _replace(
                    record,
                    id=f"mem_{record.kind.value}_{uuid.uuid4().hex}",
                    contradicted_by=merge_links(record.contradicted_by, (existing_same.id,)),
                )

        result = await resolver.resolve(record, report) if report.conflicting else None
        resolution = result.resolution if result else ConflictResolution.NONE
        if resolution is ConflictResolution.REJECT:
            return MemoryWriteResult(
                status="REJECTED",
                memory_id=record.id,
                record=record,
                reason=report.reason or "memory conflict rejected",
            )

        outcome_status = "CREATED"
        if resolution is ConflictResolution.FLAG:
            assert result is not None, "FLAG resolution requires a resolver result"
            record = _replace(
                record,
                id=f"mem_{record.kind.value}_{uuid.uuid4().hex}",
                contradicted_by=merge_links(
                    record.contradicted_by, tuple(c.id for c in result.superseded)
                ),
            )
            outcome_status = "CONFLICT"
        elif resolution is ConflictResolution.SUPERSEDE:
            assert result is not None, "SUPERSEDE resolution requires a resolver result"
            record = _replace(
                record,
                supersedes=merge_links(record.supersedes, tuple(c.id for c in result.superseded)),
            )
            await self._merge_superseded(tuple(item.id for item in result.superseded), record.id)
            outcome_status = "SUPERSEDED"

        source_task = None
        source_session = None
        if provenance.source_id:
            if provenance.source_type == SourceType.TASK:
                source_task = provenance.source_id
            elif provenance.source_type == SourceType.SESSION:
                source_session = provenance.source_id

        metadata = record_metadata(record, provenance, now)
        text_content = " ".join(filter(None, (record.content, record.summary)))
        cursor = await self._db.execute(
            "INSERT INTO memories "
            "(id, scope, content, text_content, kind, source_task_id, "
            " source_session_id, created_at, updated_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO NOTHING",
            (
                record.id,
                record.scope.value,
                record.content,
                text_content,
                record.kind.value,
                source_task,
                source_session,
                now,
                now,
                json.dumps(metadata, default=str),
            ),
        )
        if getattr(cursor, "rowcount", 1) == 0:
            stored = await self._get_record(record.id)
            return MemoryWriteResult(
                status="DEDUPED",
                memory_id=record.id,
                record=stored or record,
                reason="an equivalent write already exists",
            )
        if self._eager_index():
            await self._index_embedding(record)
        self._mark_mutation()
        return MemoryWriteResult(
            status=outcome_status,
            memory_id=record.id,
            record=record,
            reason=(report.reason if outcome_status in {"CONFLICT", "SUPERSEDED"} else None),
        )
