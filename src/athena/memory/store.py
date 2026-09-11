from __future__ import annotations

import json
import hashlib
import inspect
import logging
import math
import uuid
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from athena.protocol.memory import MemoryKind, MemoryRecord, MemoryScope, RetrievalMode
from athena.protocol.messages import Provenance, SourceType, TrustClass, utcnow
from athena.state.database import Database

_logger = logging.getLogger("athena.memory.store")

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
_RETRIEVAL_SCORE_KEY = f"{_NSP}:retrieval_score"

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
        _RETRIEVAL_SCORE_KEY,
    }
)


@dataclass(frozen=True)
class MemoryWriteResult:
    """Explicit durable outcome of a memory write attempt."""

    status: str
    memory_id: str | None = None
    record: MemoryRecord | None = None
    reason: str | None = None


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

    def __init__(self, db: Database, *, embedding_provider: Any = None) -> None:
        self._db = db
        self._generation = 0
        self._embedding_provider = embedding_provider

    @property
    def embedding_provider(self) -> Any:
        """Configured optional provider, exposed for retrieval checks."""
        return self._embedding_provider

    @property
    def generation(self) -> int:
        """Process-local revision for compiler retrieval caches."""
        return self._generation

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
        """Compatibility wrapper returning the effective record.

        New model-facing callers must use :meth:`save_with_outcome`; this
        wrapper preserves the historical store API for internal pipelines.
        """
        outcome = await self.save_with_outcome(record)
        return outcome.record or record

    async def save_with_outcome(self, record: MemoryRecord) -> MemoryWriteResult:
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
                return MemoryWriteResult(
                    status="REJECTED",
                    memory_id=existing_same.id,
                    record=existing_same,
                    reason="incoming memory has lower trust than the existing record",
                )
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
                id=new_memory_id(record.kind),
                contradicted_by=_merge_links(
                    record.contradicted_by,
                    tuple(c.id for c in result.superseded),
                ),
            )
            outcome_status = "CONFLICT"
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
            outcome_status = "SUPERSEDED"

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
        cursor = await self._db.execute(sql, params)
        if getattr(cursor, "rowcount", 1) == 0:
            stored = await self.get(record.id)
            return MemoryWriteResult(
                status="DEDUPED",
                memory_id=record.id,
                record=stored or record,
                reason="an equivalent write already exists",
            )
        if bool(getattr(self._embedding_provider, "eager_index", True)):
            await self._index_embedding(record)
        self._generation += 1
        return MemoryWriteResult(
            status=outcome_status,
            memory_id=record.id,
            record=record,
            reason=(report.reason if outcome_status in {"CONFLICT", "SUPERSEDED"} else None),
        )

    async def _index_embedding(self, record: MemoryRecord) -> bool:
        provider = self._embedding_provider
        if provider is None or not callable(getattr(provider, "embed", None)):
            return False
        text = " ".join(filter(None, (record.content, record.summary)))
        try:
            raw = provider.embed(text)
            vector = await raw if inspect.isawaitable(raw) else raw
            values = tuple(float(item) for item in vector)
            if not values or not all(math.isfinite(item) for item in values):
                raise ValueError("embedding provider returned an invalid vector")
            model = str(getattr(provider, "model", type(provider).__name__))
            version = str(getattr(provider, "version", "1"))
            await self._db.execute(
                "INSERT INTO memory_embeddings(memory_id, content_hash, embedding_model, "
                "embedding_version, vector, created_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(memory_id) DO UPDATE SET content_hash=excluded.content_hash, "
                "embedding_model=excluded.embedding_model, embedding_version=excluded.embedding_version, "
                "vector=excluded.vector, created_at=excluded.created_at",
                (
                    record.id,
                    memory_content_hash(record),
                    model,
                    version,
                    json.dumps(values),
                    utcnow().isoformat(),
                ),
            )
            return True
        except Exception as exc:
            # Embeddings are derived retrieval acceleration; canonical memory
            # remains durable even when an optional provider is unavailable.
            _logger.warning("memory embedding index failed for %s: %s", record.id, exc)
            return False

    async def ensure_embeddings(
        self,
        scope: MemoryScope | None = None,
        scope_id: str | None = None,
        *,
        tags: Sequence[str] | None = None,
        limit: int = 5000,
        include_inactive: bool = False,
        include_conflicts: bool = False,
    ) -> int:
        """Backfill missing/stale vectors when semantic retrieval is requested.

        Vectors are a derived index, not canonical memory. This method keeps
        ordinary writes fast and lets an explicitly semantic query pay the
        one-time local model initialization/backfill cost. Content hashes and
        provider identity make model/content changes invalidate old vectors.
        """
        provider = self._embedding_provider
        if provider is None or not callable(getattr(provider, "embed", None)):
            return 0
        scope_where, params = await self._scope_where(
            scope,
            scope_id,
            tags,
            include_inactive=include_inactive,
            include_conflicts=include_conflicts,
        )
        where = f"WHERE {scope_where}" if scope_where else ""
        rows = await self._db.fetch_all(
            "SELECT m.*, e.content_hash AS embedding_content_hash, "
            "e.embedding_model, e.embedding_version "
            "FROM memories m LEFT JOIN memory_embeddings e ON e.memory_id = m.id "
            f"{where} ORDER BY m.created_at DESC LIMIT ?",
            [*params, max(1, min(int(limit), 10_000))],
        )
        model = str(getattr(provider, "model", type(provider).__name__))
        version = str(getattr(provider, "version", "1"))
        indexed = 0
        for row in rows:
            record = _row_to_record(row)
            if (
                row.get("embedding_content_hash") == memory_content_hash(record)
                and row.get("embedding_model") == model
                and row.get("embedding_version") == version
            ):
                continue
            if await self._index_embedding(record):
                indexed += 1
        return indexed

    async def retrieve_by_embedding(
        self,
        vector: Sequence[float],
        scope: MemoryScope | None,
        scope_id: str | None,
        limit: int,
        tags: Sequence[str] | None = None,
        *,
        include_inactive: bool = False,
        include_conflicts: bool = False,
    ) -> list[tuple[MemoryRecord, float]]:
        scope_w, params = await self._scope_where(
            scope,
            scope_id,
            tags,
            include_inactive=include_inactive,
            include_conflicts=include_conflicts,
        )
        where = f"WHERE {scope_w}" if scope_w else ""
        rows = await self._db.fetch_all(
            "SELECT m.*, e.vector AS embedding_vector FROM memories m "
            "JOIN memory_embeddings e ON e.memory_id = m.id "
            f"{where}",
            params,
        )
        query = tuple(float(item) for item in vector)
        scored: list[tuple[MemoryRecord, float]] = []
        for row in rows:
            try:
                candidate = tuple(float(item) for item in json.loads(row["embedding_vector"]))
                score = _cosine_similarity(query, candidate)
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue
            scored.append((_row_to_record(row), score))
        scored.sort(key=lambda item: (item[1], item[0].created_at, item[0].id), reverse=True)
        return scored[: max(1, int(limit))]

    async def retrieve_by_embedding_scopes(
        self,
        vector: Sequence[float],
        scopes: Sequence[tuple[MemoryScope, str | None]],
        limit: int,
        tags: Sequence[str] | None = None,
        *,
        include_inactive: bool = False,
        include_conflicts: bool = False,
    ) -> list[tuple[MemoryRecord, float]]:
        rows: list[tuple[MemoryRecord, float]] = []
        for scope, scope_id in scopes:
            rows.extend(
                await self.retrieve_by_embedding(
                    vector,
                    scope,
                    scope_id,
                    limit,
                    tags,
                    include_inactive=include_inactive,
                    include_conflicts=include_conflicts,
                )
            )
        by_id: dict[str, tuple[MemoryRecord, float]] = {}
        for record, score in rows:
            if record.id not in by_id or score > by_id[record.id][1]:
                by_id[record.id] = (record, score)
        return sorted(
            by_id.values(),
            key=lambda item: (item[1], item[0].created_at, item[0].id),
            reverse=True,
        )[: max(1, int(limit))]

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
        changed = cursor.rowcount is not None and cursor.rowcount > 0
        if changed:
            self._generation += 1
        return changed

    async def list_pending_candidates(self, limit: int = 100) -> list[MemoryRecord]:
        """List agent-derived memory candidates awaiting deliberate review."""
        rows = await self._db.fetch_all(
            "SELECT * FROM memories WHERE json_extract(metadata, '$.pending_promotion') = 1 "
            "ORDER BY created_at ASC LIMIT ?",
            (max(1, int(limit)),),
        )
        return [_row_to_record(row) for row in rows]

    async def promote_pending_candidate(
        self,
        id: str,
        *,
        scope: MemoryScope,
        scope_id: str | None = None,
    ) -> MemoryRecord | None:
        """Promote one pending candidate after an operator decision."""
        record = await self.get(id)
        if record is None or (record.metadata or {}).get("pending_promotion") is not True:
            return None
        target_scope_id = scope_id
        if target_scope_id is None and scope in (MemoryScope.TASK, MemoryScope.SESSION):
            target_scope_id = record.source.source_id if record.source else None
        metadata = {
            **dict(record.metadata),
            "pending_promotion": False,
            "promotion": "promoted",
            **({"scope_id": target_scope_id} if target_scope_id else {}),
        }
        promoted = _replace(record, scope=scope, metadata=metadata)
        source = promoted.source or Provenance(
            source_type=SourceType.RUNTIME,
            source_id=promoted.id,
            trust=promoted.trust,
        )
        now = utcnow().isoformat()
        canonical_metadata = self._record_metadata(promoted, source, now)
        text_content = " ".join(filter(None, (promoted.content, promoted.summary)))
        await self._db.execute(
            "UPDATE memories SET scope = ?, text_content = ?, metadata = ?, updated_at = ? "
            "WHERE id = ?",
            (
                scope.value,
                text_content,
                json.dumps(canonical_metadata, default=str),
                now,
                id,
            ),
        )
        self._generation += 1
        return await self.get(id)

    async def discard_pending_candidate(self, id: str) -> bool:
        record = await self.get(id)
        if record is None or (record.metadata or {}).get("pending_promotion") is not True:
            return False
        return await self.delete(id)

    async def expire_pending_candidates(self, before: datetime) -> int:
        cursor = await self._db.execute(
            "DELETE FROM memories WHERE json_extract(metadata, '$.pending_promotion') = 1 "
            "AND created_at < ?",
            (before.isoformat(),),
        )
        changed = int(cursor.rowcount or 0)
        if changed:
            self._generation += 1
        return changed

    async def compact_pending_candidates(self, limit: int = 512) -> int:
        """Retain only the newest bounded set of pending candidates."""
        keep = max(1, int(limit))
        rows = await self._db.fetch_all(
            "SELECT id FROM memories "
            "WHERE json_extract(metadata, '$.pending_promotion') = 1 "
            "ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?",
            (keep,),
        )
        removed = 0
        for row in rows:
            if await self.discard_pending_candidate(str(row["id"])):
                removed += 1
        return removed

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
        if record.scope in (
            MemoryScope.TASK,
            MemoryScope.SESSION,
            MemoryScope.JOB,
            MemoryScope.USER,
        ):
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
        mode: RetrievalMode | str = RetrievalMode.RELEVANCE,
        limit: int = 10,
        include_inactive: bool = False,
        include_conflicts: bool = False,
    ) -> list[MemoryRecord]:
        from athena.memory.retrieval import MemoryRetriever

        return await MemoryRetriever(self).retrieve(
            query=query,
            scope=scope or MemoryScope.SESSION,
            scope_id=scope_id,
            mode=mode,
            limit=limit,
            tags=tags,
            include_inactive=include_inactive,
            include_conflicts=include_conflicts,
        )

    async def search(
        self,
        query: str,
        limit: int = 10,
        *,
        scope: MemoryScope | None = MemoryScope.SESSION,
        scope_id: str | None = None,
        mode: RetrievalMode | str = RetrievalMode.RELEVANCE,
        tags: Sequence[str] | None = None,
        include_inactive: bool = False,
        include_conflicts: bool = False,
    ) -> list[MemoryRecord]:
        from athena.memory.retrieval import MemoryRetriever

        return await MemoryRetriever(self).retrieve(
            query=query,
            scope=scope,
            scope_id=scope_id,
            mode=mode,
            limit=limit,
            tags=tags,
            include_inactive=include_inactive,
            include_conflicts=include_conflicts,
        )

    async def search_scopes(
        self,
        query: str,
        scopes: Sequence[tuple[MemoryScope, str | None]],
        *,
        limit: int = 10,
        mode: RetrievalMode | str = RetrievalMode.RELEVANCE,
        tags: Sequence[str] | None = None,
        include_inactive: bool = False,
        include_conflicts: bool = False,
    ) -> list[MemoryRecord]:
        """Search several authority scopes with one retrieval operation."""
        from athena.memory.retrieval import MemoryRetriever

        return await MemoryRetriever(self).retrieve_scopes(
            query=query,
            scopes=scopes,
            mode=mode,
            limit=limit,
            tags=tags,
            include_inactive=include_inactive,
            include_conflicts=include_conflicts,
        )

    async def retrieve_scopes_weighted(
        self,
        query: str,
        scopes: Sequence[tuple[MemoryScope, str | None]],
        *,
        limit: int = 10,
        mode: RetrievalMode | str = RetrievalMode.RELEVANCE,
        tags: Sequence[str] | None = None,
        weights: Mapping[str, float] | None = None,
        include_inactive: bool = False,
        include_conflicts: bool = False,
    ) -> list[MemoryRecord]:
        """Scope-weighted retrieval (P1-12): authority order participates in
        the ranking, so a session-local memory outranks a user-global one on
        equal text overlap. ``weights`` maps scope VALUE names (upper-case)
        to multipliers; unlisted scopes default to 0.0 weight.
        """
        from athena.memory.retrieval import MemoryRetriever

        return await MemoryRetriever(self).retrieve_scopes_weighted(
            query=query,
            scopes=scopes,
            mode=mode,
            limit=limit,
            tags=tags,
            weights=weights,
            include_inactive=include_inactive,
            include_conflicts=include_conflicts,
        )

    # ---- retrieval SQL (owned by the store; the retriever only re-ranks) ----

    async def _scope_where(
        self,
        scope: MemoryScope | None,
        scope_id: str | None,
        tags: Sequence[str] | None = None,
        *,
        include_inactive: bool = False,
        include_conflicts: bool = False,
    ) -> tuple[str, list[Any]]:
        conds: list[str] = []
        params: list[Any] = []
        if not include_inactive:
            # Pending agent-derived candidates are review material, not
            # ordinary context. Explicit USER_CONTENT records use
            # pending_promotion=false and remain immediately retrievable.
            conds.append("COALESCE(json_extract(m.metadata, '$.pending_promotion'), 0) != 1")
            # Temporal validity is retrieval policy, not merely display
            # metadata. SQLite's datetime() comparison keeps the policy in
            # UTC even when records contain explicit offsets.
            conds.append(
                "(json_extract(m.metadata, '$._athena:valid_from') IS NULL "
                "OR datetime(json_extract(m.metadata, '$._athena:valid_from')) <= datetime('now'))"
            )
            conds.append(
                "(json_extract(m.metadata, '$._athena:valid_until') IS NULL "
                "OR datetime(json_extract(m.metadata, '$._athena:valid_until')) >= datetime('now'))"
            )
            # A superseded record is historical evidence, not current
            # context. The relation is stored on the successor so this also
            # works when the old record predates the current schema.
            conds.append(
                "NOT EXISTS (SELECT 1 FROM memories successor, "
                "json_each(COALESCE(json_extract(successor.metadata, "
                "'$._athena:supersedes'), '[]')) supersession "
                "WHERE supersession.value = m.id)"
            )
        if not include_conflicts:
            conds.append(
                "COALESCE(json_array_length(json_extract(m.metadata, "
                "'$._athena:contradicted_by')), 0) = 0"
            )
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
        *,
        include_inactive: bool = False,
        include_conflicts: bool = False,
    ) -> list[MemoryRecord]:
        scope_w, params = await self._scope_where(
            scope,
            scope_id,
            tags,
            include_inactive=include_inactive,
            include_conflicts=include_conflicts,
        )
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
        *,
        include_inactive: bool = False,
        include_conflicts: bool = False,
    ) -> list[MemoryRecord]:
        match = self.sanitize_match(query)
        if not match:
            return []
        scope_w, params = await self._scope_where(
            scope,
            scope_id,
            tags,
            include_inactive=include_inactive,
            include_conflicts=include_conflicts,
        )
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

    async def retrieve_by_fts_scopes(
        self,
        query: str,
        scopes: Sequence[tuple[MemoryScope, str | None]],
        limit: int,
        tags: Sequence[str] | None = None,
        *,
        include_inactive: bool = False,
        include_conflicts: bool = False,
    ) -> list[MemoryRecord]:
        match = self.sanitize_match(query)
        if not match or not scopes:
            return []
        groups: list[str] = []
        group_params: list[Any] = []
        for scope, scope_id in scopes:
            parts = ["m.scope = ?"]
            group_params.append(scope.value)
            if scope_id:
                parts.append("json_extract(m.metadata, '$._athena:scope_id') = ?")
                group_params.append(scope_id)
            groups.append("(" + " AND ".join(parts) + ")")
        state_where, state_params = await self._scope_where(
            None,
            None,
            tags,
            include_inactive=include_inactive,
            include_conflicts=include_conflicts,
        )
        where = ["memories_fts MATCH ?", state_where, "(" + " OR ".join(groups) + ")"]
        params: list[Any] = [match, *state_params, *group_params]
        params.append(limit)
        return await self._fetch_records(
            "SELECT m.* FROM memories_fts "
            "JOIN memories m ON m.rowid = memories_fts.rowid "
            "WHERE " + " AND ".join(where) + " ORDER BY bm25(memories_fts) LIMIT ?",
            params,
        )

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


def memory_content_hash(record: MemoryRecord) -> str:
    """Hash the exact canonical text represented by a stored vector."""
    content = " ".join(filter(None, (record.content, record.summary)))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


__all__ = ["MemoryStore", "MemoryWriteResult", "memory_content_hash", "new_memory_id"]
