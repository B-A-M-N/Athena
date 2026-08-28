from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Mapping, Sequence

from athena.protocol.memory import MemoryKind, MemoryRecord, MemoryScope, RetrievalMode
from athena.protocol.messages import Provenance, SourceType, TrustClass, utcnow
from athena.state.database import Database

_NSP = "_athena"
_TRUST_KEY = f"{_NSP}:trust"
_SCOPE_ID_KEY = f"{_NSP}:scope_id"
_SUMMARY_KEY = f"{_NSP}:summary"
_PROVENANCE_KEY = f"{_NSP}:provenance"
_KIND_KEY = f"{_NSP}:kind"
_UPDATED_AT_KEY = f"{_NSP}:updated_at"
_RETRIEVAL_MODE_KEY = f"{_NSP}:retrieval_mode"
_SUBJECT_KEY = f"{_NSP}:subject"
_TAGS_KEY = f"{_NSP}:tags"
_SOURCE_REFS_KEY = f"{_NSP}:source_refs"
_CONFIDENCE_KEY = f"{_NSP}:confidence"
_VALID_FROM_KEY = f"{_NSP}:valid_from"
_VALID_UNTIL_KEY = f"{_NSP}:valid_until"
_SUPERSEDES_KEY = f"{_NSP}:supersedes"
_CONTRADICTED_BY_KEY = f"{_NSP}:contradicted_by"

_NS_KEYS = frozenset(
    {
        _TRUST_KEY,
        _SCOPE_ID_KEY,
        _SUMMARY_KEY,
        _PROVENANCE_KEY,
        _KIND_KEY,
        _UPDATED_AT_KEY,
        _RETRIEVAL_MODE_KEY,
        _SUBJECT_KEY,
        _TAGS_KEY,
        _SOURCE_REFS_KEY,
        _CONFIDENCE_KEY,
        _VALID_FROM_KEY,
        _VALID_UNTIL_KEY,
        _SUPERSEDES_KEY,
        _CONTRADICTED_BY_KEY,
    }
)


def new_memory_id(kind: MemoryKind | str = MemoryKind.SEMANTIC) -> str:
    """Generate a stable, opaque memory identifier."""
    prefix = getattr(kind, "value", kind)
    return f"mem_{prefix}_{uuid.uuid4().hex}"


def _prov_to_dict(p: Provenance) -> dict[str, Any]:
    return {
        "source_type": p.source_type.value,
        "source_id": p.source_id,
        "trust": p.trust.value,
        "scope": p.scope,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _prov_from_dict(d: Mapping[str, Any]) -> Provenance:
    return Provenance(
        source_type=SourceType(d.get("source_type", SourceType.RUNTIME.value)),
        source_id=d.get("source_id"),
        trust=TrustClass(d.get("trust", TrustClass.AGENT_CURATED.value)),
        scope=d.get("scope"),
        created_at=_parse_dt(d.get("created_at")),
    )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _strip_ns(md: dict[str, Any]) -> Mapping[str, Any]:
    if not md:
        return {}
    return {k: v for k, v in md.items() if k not in _NS_KEYS}


def _row_to_record(row: Mapping[str, Any]) -> MemoryRecord:
    md: dict[str, Any] = {}
    try:
        md = json.loads(row.get("metadata") or "{}")
    except Exception:
        md = {}
    trust_value = md.get(_TRUST_KEY, TrustClass.AGENT_CURATED.value)
    trust = (
        TrustClass(trust_value)
        if trust_value and trust_value in TrustClass._value2member_map_
        else TrustClass.AGENT_CURATED
    )
    prov_raw = md.get(_PROVENANCE_KEY)
    if isinstance(prov_raw, dict):
        prov = _prov_from_dict(prov_raw)
    else:
        prov = Provenance(
            source_type=SourceType.RUNTIME, source_id=row.get("source_task_id"), trust=trust
        )
    kind_raw = md.get(_KIND_KEY, row.get("kind"))
    kind = (
        MemoryKind(kind_raw)
        if kind_raw and kind_raw in MemoryKind._value2member_map_
        else MemoryKind.WORKING
    )
    created = _parse_dt(row.get("created_at")) or utcnow()
    updated = _parse_dt(md.get(_UPDATED_AT_KEY)) or _parse_dt(row.get("updated_at"))
    retrieval_raw = md.get(_RETRIEVAL_MODE_KEY)
    retrieval_mode = (
        RetrievalMode(retrieval_raw)
        if retrieval_raw and retrieval_raw in RetrievalMode._value2member_map_
        else None
    )
    valid_from = _parse_dt(md.get(_VALID_FROM_KEY))
    valid_until = _parse_dt(md.get(_VALID_UNTIL_KEY))
    conf = md.get(_CONFIDENCE_KEY)
    return MemoryRecord(
        id=row["id"],
        kind=kind,
        scope=_scope_from_row(row),
        content=row.get("content") or "",
        summary=md.get(_SUMMARY_KEY),
        source=prov,
        trust=trust,
        created_at=created,
        updated_at=updated,
        metadata=_strip_ns(md),
        retrieval_mode=retrieval_mode,
        subject=md.get(_SUBJECT_KEY),
        tags=_tuple_of(md.get(_TAGS_KEY)),
        source_refs=_tuple_of(md.get(_SOURCE_REFS_KEY)),
        confidence=float(conf) if isinstance(conf, (int, float)) else None,
        valid_from=valid_from,
        valid_until=valid_until,
        supersedes=_tuple_of(md.get(_SUPERSEDES_KEY)),
        contradicted_by=_tuple_of(md.get(_CONTRADICTED_BY_KEY)),
    )


def _tuple_of(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if v)
    return ()


_TRUST_RANK: dict[TrustClass, int] = {
    TrustClass.AUTHORITY: 5,
    TrustClass.CONFIGURED_INSTRUCTION: 4,
    TrustClass.USER_CONTENT: 3,
    TrustClass.AGENT_CURATED: 2,
    TrustClass.EXTERNAL_CONTENT: 1,
    TrustClass.UNTRUSTED: 0,
}


def _trust_rank(t: TrustClass | None) -> int:
    return _TRUST_RANK.get(t or TrustClass.AGENT_CURATED, 0)


def _merge_links(current: Sequence[str], additional: Sequence[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for v in (*current, *additional):
        if v and v not in seen:
            seen.append(v)
    return tuple(seen)


def _replace(record: MemoryRecord, **kwargs: Any) -> MemoryRecord:
    from dataclasses import replace

    return replace(record, **kwargs)


def _scope_from_row(row: Mapping[str, Any]) -> MemoryScope:
    raw = row.get("scope")
    if raw and raw in MemoryScope._value2member_map_:
        return MemoryScope(raw)
    return MemoryScope.PROJECT


class MemoryStore:
    """Async persistence for :class:`MemoryRecord` over the ``memories`` table.

    The ``memories`` schema stores ``scope`` as a single text column. Trust,
    summary, provenance, the runtime memory kind, and the logical ``scope_id``
    are persisted under namespaced ``metadata`` keys so a full record can be
    reconstructed without altering the database schema. FTS is maintained by
    the ``memories_ai`` trigger on the external-content ``memories_fts`` table:
    writing ``text_content`` on the row is sufficient because the trigger
    mirrors it into the FTS index.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def _record_metadata(
        self,
        record: MemoryRecord,
        prov: Provenance,
        now: str,
        *,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        trust = record.trust if record.source is None else prov.trust
        md = dict(record.metadata)
        md[_TRUST_KEY] = trust.value
        md[_KIND_KEY] = record.kind.value
        md[_SCOPE_ID_KEY] = self._scope_id(record)
        if record.summary:
            md[_SUMMARY_KEY] = record.summary
        md[_PROVENANCE_KEY] = _prov_to_dict(prov)
        md[_UPDATED_AT_KEY] = now
        if record.retrieval_mode is not None:
            md[_RETRIEVAL_MODE_KEY] = record.retrieval_mode.value
        if record.subject:
            md[_SUBJECT_KEY] = record.subject
        if record.tags:
            md[_TAGS_KEY] = list(record.tags)
        if record.source_refs:
            md[_SOURCE_REFS_KEY] = list(record.source_refs)
        if record.confidence is not None:
            md[_CONFIDENCE_KEY] = record.confidence
        if record.valid_from is not None:
            md[_VALID_FROM_KEY] = record.valid_from.isoformat()
        if record.valid_until is not None:
            md[_VALID_UNTIL_KEY] = record.valid_until.isoformat()
        if record.supersedes:
            md[_SUPERSEDES_KEY] = list(record.supersedes)
        if record.contradicted_by:
            md[_CONTRADICTED_BY_KEY] = list(record.contradicted_by)
        if extra:
            md.update(extra)
        return md

    async def save(self, record: MemoryRecord) -> MemoryRecord:
        from athena.memory.conflicts import (
            ConflictResolution,
            MemoryConflictResolver,
        )

        trust = record.trust
        if record.source is not None:
            trust = record.source.trust
        prov = record.source or Provenance(source_type=SourceType.RUNTIME, trust=trust)
        now = utcnow().isoformat()

        resolver = MemoryConflictResolver(self)
        report = await resolver.detect_conflict(record)

        existing_same = await self.get(record.id)
        if existing_same is not None:
            existing_rank = _trust_rank(existing_same.trust)
            incoming_rank = _trust_rank(record.trust)
            if incoming_rank < existing_rank:
                return record
            if incoming_rank == existing_rank:
                record = _replace(
                    record,
                    id=new_memory_id(record.kind),
                    contradicted_by=_merge_links(
                        record.contradicted_by,
                        (existing_same.id,),
                    ),
                )

        result = await resolver.resolve(record, report) if report.conflicting else None

        resolution = result.resolution if result else ConflictResolution.NONE
        if resolution is ConflictResolution.REJECT:
            return record

        if resolution is ConflictResolution.FLAG:
            assert result is not None, "FLAG resolution requires a resolver result"
            record = _replace(
                record,
                id=new_memory_id(record.kind),
                contradicted_by=_merge_links(
                    record.contradicted_by,
                    tuple(c.id for c in result.superseded),
                ),
            )
        elif resolution is ConflictResolution.SUPERSEDE:
            assert result is not None, "SUPERSEDE resolution requires a resolver result"
            record = _replace(
                record,
                supersedes=_merge_links(
                    record.supersedes,
                    tuple(c.id for c in result.superseded),
                ),
            )
            await self._merge_superseded(
                tuple(t.id for t in result.superseded),
                record.id,
            )

        source_task = None
        source_session = None
        if prov.source_id:
            if prov.source_type == SourceType.TASK:
                source_task = prov.source_id
            elif prov.source_type == SourceType.SESSION:
                source_session = prov.source_id

        md = self._record_metadata(record, prov, now)
        text_content = " ".join(filter(None, (record.content, record.summary)))

        sql = (
            "INSERT INTO memories "
            "(id, scope, content, text_content, kind, source_task_id, "
            " source_session_id, created_at, updated_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO NOTHING"
        )
        params = (
            record.id,
            record.scope.value,
            record.content,
            text_content,
            record.kind.value,
            source_task,
            source_session,
            now,
            now,
            json.dumps(md, default=str),
        )
        await self._db.execute(sql, params)
        return record

    async def _merge_superseded(self, superseded_ids: Sequence[str], by_id: str) -> None:
        for old_id in superseded_ids:
            old = await self.get(old_id)
            if old is None:
                continue
            md = self._record_metadata(
                old,
                old.source or Provenance(source_type=SourceType.RUNTIME, trust=old.trust),
                utcnow().isoformat(),
                extra={
                    _CONTRADICTED_BY_KEY: _merge_links(
                        old.contradicted_by,
                        (by_id,),
                    ),
                },
            )
            await self._db.execute(
                "UPDATE memories SET metadata = ? WHERE id = ?",
                (json.dumps(md, default=str), old_id),
            )

    async def get(self, id: str) -> MemoryRecord | None:
        row = await self._db.fetch_one("SELECT * FROM memories WHERE id = ?", (id,))
        if row is None:
            return None
        return _row_to_record(row)

    async def delete(self, id: str) -> bool:
        cursor = await self._db.execute("DELETE FROM memories WHERE id = ?", (id,))
        return cursor.rowcount is not None and cursor.rowcount > 0

    async def list_by_scope(self, scope: MemoryScope, scope_id: str | None) -> list[MemoryRecord]:
        conditions = ["scope = ?"]
        params: list[Any] = [scope.value]
        if scope_id:
            conditions.append(f"json_extract(metadata, '$.{_SCOPE_ID_KEY}') = ?")
            params.append(scope_id)
        sql = f"SELECT * FROM memories WHERE {' AND '.join(conditions)} ORDER BY created_at DESC"
        rows = await self._db.fetch_all(sql, params)
        return [_row_to_record(r) for r in rows]

    async def list_by_kind(self, kind: MemoryKind) -> list[MemoryRecord]:
        rows = await self._db.fetch_all(
            "SELECT * FROM memories WHERE kind = ? ORDER BY created_at DESC",
            (kind.value,),
        )
        return [_row_to_record(r) for r in rows]

    async def count(self) -> int:
        row = await self._db.fetch_one("SELECT COUNT(*) AS c FROM memories")
        return int(row["c"]) if row else 0

    @staticmethod
    def _scope_id(record: MemoryRecord) -> str | None:
        if "scope_id" in record.metadata:
            return str(record.metadata["scope_id"])
        if record.scope in (MemoryScope.TASK, MemoryScope.SESSION):
            return record.source.source_id if record.source else None
        return None

    # ---- capability surface (aligns with MemoryCapability's injected handle) ----

    async def recall(
        self,
        query: str,
        tags: Sequence[str] | None = None,
        *,
        scope: MemoryScope | None = None,
        scope_id: str | None = None,
        mode: RetrievalMode | str = RetrievalMode.SEMANTIC,
        limit: int = 10,
    ) -> list[MemoryRecord]:
        from athena.memory.retrieval import MemoryRetriever

        return await MemoryRetriever(self).retrieve(
            query=query,
            scope=scope or MemoryScope.SESSION,
            scope_id=scope_id,
            mode=mode,
            limit=limit,
            tags=tags,
        )

    async def search(
        self,
        query: str,
        limit: int = 10,
        *,
        scope: MemoryScope | None = MemoryScope.SESSION,
        scope_id: str | None = None,
        mode: RetrievalMode | str = RetrievalMode.SEMANTIC,
        tags: Sequence[str] | None = None,
    ) -> list[MemoryRecord]:
        from athena.memory.retrieval import MemoryRetriever

        return await MemoryRetriever(self).retrieve(
            query=query,
            scope=scope,
            scope_id=scope_id,
            mode=mode,
            limit=limit,
            tags=tags,
        )

    # ---- retrieval SQL (owned by the store; the retriever only re-ranks) ----

    async def _scope_where(
        self,
        scope: MemoryScope | None,
        scope_id: str | None,
        tags: Sequence[str] | None = None,
    ) -> tuple[str, list[Any]]:
        conds: list[str] = []
        params: list[Any] = []
        if scope is not None:
            conds.append("m.scope = ?")
            params.append(scope.value)
        if scope_id:
            conds.append("json_extract(m.metadata, '$._athena:scope_id') = ?")
            params.append(scope_id)
        for tag in tags or ():
            if tag:
                conds.append("json_extract(m.metadata, '$._athena:tags') LIKE ?")
                params.append(f'%"{tag}"%')
        return (" AND ".join(conds), params)

    async def _fetch_records(self, sql: str, params: list[Any]) -> list[MemoryRecord]:
        rows = await self._db.fetch_all(sql, params)
        return [_row_to_record(r) for r in rows]

    async def retrieve_by_recency(
        self,
        scope: MemoryScope | None,
        scope_id: str | None,
        limit: int,
        tags: Sequence[str] | None = None,
    ) -> list[MemoryRecord]:
        scope_w, params = await self._scope_where(scope, scope_id, tags)
        where = f"WHERE {scope_w}" if scope_w else ""
        sql = f"SELECT m.* FROM memories m {where} ORDER BY m.created_at DESC LIMIT ?"
        params.append(limit)
        return await self._fetch_records(sql, params)

    async def retrieve_by_fts(
        self,
        query: str,
        scope: MemoryScope | None,
        scope_id: str | None,
        limit: int,
        tags: Sequence[str] | None = None,
    ) -> list[MemoryRecord]:
        match = self.sanitize_match(query)
        if not match:
            return []
        scope_w, params = await self._scope_where(scope, scope_id, tags)
        where_parts = ["memories_fts MATCH ?"]
        if scope_w:
            where_parts.append(scope_w)
        where = " AND ".join(where_parts)
        params.insert(0, match)
        sql = (
            "SELECT m.* FROM memories_fts "
            "JOIN memories m ON m.rowid = memories_fts.rowid "
            f"WHERE {where} ORDER BY bm25(memories_fts) LIMIT ?"
        )
        params.append(limit)
        return await self._fetch_records(sql, params)

    @staticmethod
    def sanitize_match(query: str) -> str:
        """Reduce free text to a safe FTS5 OR-expression of quoted barewords."""
        import re as _re

        tokens = _re.findall(r"[a-z0-9_']+", query.lower())
        seen: list[str] = []
        for t in tokens:
            if t not in seen:
                seen.append(t)
        return " OR ".join(f'"{t}"' for t in seen[:32])


__all__ = ["MemoryStore", "new_memory_id"]
