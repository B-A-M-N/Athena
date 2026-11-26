from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Mapping, Sequence

from athena.protocol.memory import MemoryKind, MemoryRecord, MemoryScope, RetrievalMode
from athena.protocol.messages import Provenance, SourceType, utcnow
from athena.memory.candidate_lifecycle import MemoryCandidateLifecycle
from athena.memory.embedding_index import MemoryEmbeddingIndex, memory_content_hash
from athena.memory.record_codec import (
    CONTRADICTED_BY_KEY,
    record_metadata,
    row_to_record,
)
from athena.memory.queries import MemoryRecordQuery
from athena.memory.retrieval_store import MemoryRetrievalStore
from athena.memory.writes import MemoryWriteCoordinator, MemoryWriteResult, merge_links
from athena.state.database import Database


def new_memory_id(kind: MemoryKind | str = MemoryKind.SEMANTIC) -> str:
    """Generate a stable, opaque memory identifier."""
    prefix = getattr(kind, "value", kind)
    return f"mem_{prefix}_{uuid.uuid4().hex}"


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
        self._queries = MemoryRecordQuery(db)
        self._retrieval_store = MemoryRetrievalStore(
            db,
            record_from_row=row_to_record,
        )
        self._embedding_index = MemoryEmbeddingIndex(
            db=db,
            provider=embedding_provider,
            scope_where=self._retrieval_store.scope_where,
            record_from_row=row_to_record,
        )
        self._candidate_lifecycle = MemoryCandidateLifecycle(
            db,
            get_record=self.get,
            delete_record=self.delete,
            record_from_row=row_to_record,
            record_metadata=record_metadata,
            on_mutation=self._mark_mutation,
        )
        self._writes = MemoryWriteCoordinator(
            db,
            list_by_kind=self.list_by_kind,
            get_record=self.get,
            merge_superseded=self._merge_superseded,
            index_embedding=self._index_embedding,
            eager_index=lambda: bool(getattr(self._embedding_provider, "eager_index", True)),
            mark_mutation=self._mark_mutation,
        )

    @property
    def embedding_provider(self) -> Any:
        """Configured optional provider, exposed for retrieval checks."""
        return self._embedding_provider

    @property
    def generation(self) -> int:
        """Process-local revision for compiler retrieval caches."""
        return self._generation

    def _mark_mutation(self) -> None:
        self._generation += 1

    async def save(self, record: MemoryRecord) -> MemoryRecord:
        """Compatibility wrapper returning the effective record.

        New model-facing callers must use :meth:`save_with_outcome`; this
        wrapper preserves the historical store API for internal pipelines.
        """
        outcome = await self.save_with_outcome(record)
        return outcome.record or record

    async def save_with_outcome(self, record: MemoryRecord) -> MemoryWriteResult:
        return await self._writes.save(record)

    async def _index_embedding(self, record: MemoryRecord) -> bool:
        return await self._embedding_index.index(record)

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
        return await self._embedding_index.ensure(
            scope,
            scope_id,
            tags=tags,
            limit=limit,
            include_inactive=include_inactive,
            include_conflicts=include_conflicts,
        )

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
        return await self._embedding_index.retrieve(
            vector,
            scope,
            scope_id,
            limit,
            tags,
            include_inactive=include_inactive,
            include_conflicts=include_conflicts,
        )

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
        return await self._embedding_index.retrieve_scopes(
            vector,
            scopes,
            limit,
            tags,
            include_inactive=include_inactive,
            include_conflicts=include_conflicts,
        )

    async def _merge_superseded(self, superseded_ids: Sequence[str], by_id: str) -> None:
        for old_id in superseded_ids:
            old = await self.get(old_id)
            if old is None:
                continue
            md = record_metadata(
                old,
                old.source or Provenance(source_type=SourceType.RUNTIME, trust=old.trust),
                utcnow().isoformat(),
                extra={
                    CONTRADICTED_BY_KEY: merge_links(
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
        return await self._queries.get(id)

    async def delete(self, id: str) -> bool:
        cursor = await self._db.execute("DELETE FROM memories WHERE id = ?", (id,))
        changed = cursor.rowcount is not None and cursor.rowcount > 0
        if changed:
            self._generation += 1
        return changed

    async def list_pending_candidates(self, limit: int = 100) -> list[MemoryRecord]:
        return await self._candidate_lifecycle.list(limit)

    async def promote_pending_candidate(
        self,
        id: str,
        *,
        scope: MemoryScope,
        scope_id: str | None = None,
    ) -> MemoryRecord | None:
        return await self._candidate_lifecycle.promote(id, scope=scope, scope_id=scope_id)

    async def discard_pending_candidate(self, id: str) -> bool:
        return await self._candidate_lifecycle.discard(id)

    async def expire_pending_candidates(self, before: datetime) -> int:
        return await self._candidate_lifecycle.expire(before)

    async def compact_pending_candidates(self, limit: int = 512) -> int:
        return await self._candidate_lifecycle.compact(limit)

    async def list_by_scope(self, scope: MemoryScope, scope_id: str | None) -> list[MemoryRecord]:
        return await self._queries.list_by_scope(scope, scope_id)

    async def list_by_kind(self, kind: MemoryKind) -> list[MemoryRecord]:
        return await self._queries.list_by_kind(kind)

    async def count(self) -> int:
        return await self._queries.count()

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
        return await self._retrieval_store.retrieve_by_recency(
            scope,
            scope_id,
            limit,
            tags,
            include_inactive=include_inactive,
            include_conflicts=include_conflicts,
        )

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
        return await self._retrieval_store.retrieve_by_fts(
            query,
            scope,
            scope_id,
            limit,
            tags,
            include_inactive=include_inactive,
            include_conflicts=include_conflicts,
        )

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
        return await self._retrieval_store.retrieve_by_fts_scopes(
            query,
            scopes,
            limit,
            tags,
            include_inactive=include_inactive,
            include_conflicts=include_conflicts,
        )

    @staticmethod
    def sanitize_match(query: str) -> str:
        return MemoryRetrievalStore.sanitize_match(query)


__all__ = ["MemoryStore", "MemoryWriteResult", "memory_content_hash", "new_memory_id"]
