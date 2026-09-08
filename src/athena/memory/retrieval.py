from __future__ import annotations

import math
import re
import inspect
from typing import TYPE_CHECKING, Mapping, Sequence

from athena.memory.embeddings import SemanticRetrievalUnavailable
from athena.protocol.memory import MemoryRecord, MemoryScope, RetrievalMode

if TYPE_CHECKING:
    from athena.memory.store import MemoryStore

_TOKEN_RE = re.compile(r"[a-z0-9_']+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


class MemoryRetriever:
    """Retrieves :class:`MemoryRecord` instances by the configured mode.

    EXACT uses SQLite FTS5 ``MATCH`` against the external-content
    ``memories_fts`` index (rowid-aligned to the ``memories`` table).

    RECENCY selects by ``created_at DESC`` within the requested scope.

    RELEVANCE is lexical FTS/BM25 plus token overlap. SEMANTIC uses the
    configured embedding provider, and HYBRID combines lexical and vector
    rankings with reciprocal-rank fusion plus named authority/trust/quality
    tie-break signals. Semantic modes fail explicitly when embeddings are not
    configured; they never claim paraphrase understanding accidentally.

    All SQL for these modes is owned by :class:`~athena.memory.store.MemoryStore`
    (``retrieve_by_recency`` / ``retrieve_by_fts``); this retriever only
    orchestrates and re-ranks, so raw SQL never leaks into business logic.
    """

    def __init__(self, store: "MemoryStore") -> None:
        self._store = store

    async def retrieve(
        self,
        *,
        query: str,
        scope: MemoryScope | None,
        scope_id: str | None,
        mode: RetrievalMode | str,
        limit: int,
        tags: Sequence[str] | None = None,
    ) -> list[MemoryRecord]:
        mode = RetrievalMode(mode)
        limit = max(0, int(limit or 0))
        if limit == 0:
            return []
        if mode is RetrievalMode.RECENCY:
            return await self._store.retrieve_by_recency(scope, scope_id, limit, tags=tags)
        if mode is RetrievalMode.EXACT:
            return await self._store.retrieve_by_fts(query, scope, scope_id, limit, tags=tags)
        if mode is RetrievalMode.SEMANTIC:
            return await self._by_semantic(query, scope, scope_id, limit, tags=tags)
        if mode is RetrievalMode.HYBRID:
            return await self._by_hybrid(query, scope, scope_id, limit, tags=tags)
        return await self._by_relevance(query, scope, scope_id, limit, tags=tags)

    async def retrieve_all(self, query: str, limit: int = 10) -> list[MemoryRecord]:
        return await self._by_relevance(query, None, None, limit)

    async def retrieve_scopes(
        self,
        *,
        query: str,
        scopes: Sequence[tuple[MemoryScope, str | None]],
        mode: RetrievalMode | str,
        limit: int,
        tags: Sequence[str] | None = None,
    ) -> list[MemoryRecord]:
        mode = RetrievalMode(mode)
        limit = max(0, int(limit or 0))
        if limit == 0 or not scopes:
            return []
        if mode is RetrievalMode.RECENCY:
            # One SQL query is reserved for the common relevance/exact path;
            # recency keeps its explicit per-scope ordering semantics.
            rows: list[MemoryRecord] = []
            for scope, scope_id in scopes:
                rows.extend(
                    await self._store.retrieve_by_recency(scope, scope_id, limit, tags=tags)
                )
            return sorted(rows, key=lambda item: item.created_at, reverse=True)[:limit]
        if mode is RetrievalMode.SEMANTIC:
            return await self._by_semantic_scopes(query, scopes, limit, tags=tags)
        if mode is RetrievalMode.HYBRID:
            return await self._by_hybrid_scopes(query, scopes, limit, tags=tags)
        candidate = await self._store.retrieve_by_fts_scopes(query, scopes, limit * 8, tags=tags)
        if mode is RetrievalMode.EXACT:
            return candidate[:limit]
        return self._rank(candidate, query, limit)

    async def retrieve_scopes_weighted(
        self,
        *,
        query: str,
        scopes: Sequence[tuple[MemoryScope, str | None]],
        mode: RetrievalMode | str,
        limit: int,
        tags: Sequence[str] | None = None,
        weights: Mapping[str, float] | None = None,
    ) -> list[MemoryRecord]:
        """Rank scope-weighted: score = text_overlap * scope_weight (P1-12).

        Candidates come from the same FTS union as ``retrieve_scopes``;
        the difference is the ranking key. Text overlap stays the base
        signal (a weight can never rescue a non-matching record), and the
        scope weight is the tie-breaking authority preference — which is
        what makes "weight current session > project > user-global" a
        ranking property rather than a post-hoc sort.
        """
        mode = RetrievalMode(mode)
        limit = max(0, int(limit or 0))
        if limit == 0 or not scopes:
            return []
        if mode in (RetrievalMode.SEMANTIC, RetrievalMode.HYBRID):
            records = await self.retrieve_scopes(
                query=query, scopes=scopes, mode=mode, limit=limit * 8, tags=tags
            )
            weight_map = {str(k).lower(): float(v) for k, v in (weights or {}).items()}
            weighted_scores = [
                (
                    _quality_score(record, index=0) * weight_map.get(record.scope.value, 1.0),
                    record,
                )
                for record in records
                if weight_map.get(record.scope.value, 1.0) > 0
            ]
            weighted_scores.sort(
                key=lambda item: (item[0], item[1].created_at, item[1].id), reverse=True
            )
            return [record for _, record in weighted_scores[:limit]]
        # Weight keys are matched case-insensitively against the scope VALUE
        # ("session" / "project" / "global"), so callers may use either the
        # enum name (SESSION) or the value.
        weight_map = {str(k).lower(): float(v) for k, v in (weights or {}).items()}
        candidate = await self._store.retrieve_by_fts_scopes(query, scopes, limit * 8, tags=tags)
        qset = _tokens(query)
        scope_key = {scope.value: weight_map.get(scope.value, 0.0) for scope, _ in scopes}
        scored: list[tuple[float, MemoryRecord]] = []
        for rec in candidate:
            scope_weight = scope_key.get(rec.scope.value, 0.0)
            if scope_weight <= 0.0:
                continue
            text = " ".join((rec.content or "", rec.summary or "")).lower()
            overlap = len(qset & _tokens(text)) / len(qset) if qset else 0.0
            if overlap <= 0.0:
                continue
            scored.append((overlap * scope_weight, rec))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [rec for _, rec in scored[:limit]]

    @staticmethod
    def _rank(candidate: list[MemoryRecord], query: str, limit: int) -> list[MemoryRecord]:
        qset = _tokens(query)
        if not qset:
            return candidate[:limit]
        scored: list[tuple[float, MemoryRecord]] = []
        for rec in candidate:
            text = " ".join((rec.content or "", rec.summary or "")).lower()
            intersect = len(qset & _tokens(text))
            scored.append((intersect / len(qset), rec))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [rec for score, rec in scored if score > 0][:limit]

    async def _by_relevance(
        self,
        query: str,
        scope: MemoryScope | None,
        scope_id: str | None,
        limit: int,
        tags: Sequence[str] | None = None,
    ) -> list[MemoryRecord]:
        candidate = await self._store.retrieve_by_fts(query, scope, scope_id, limit * 8, tags=tags)
        return self._rank(candidate, query, limit)

    async def _query_embedding(self, query: str) -> Sequence[float]:
        provider = getattr(self._store, "embedding_provider", None)
        if provider is None or not callable(getattr(provider, "embed", None)):
            raise SemanticRetrievalUnavailable(
                "semantic retrieval unavailable: no MemoryEmbeddingProvider configured"
            )
        raw = provider.embed(query)
        vector = await raw if inspect.isawaitable(raw) else raw
        try:
            values = tuple(float(item) for item in vector)
        except (TypeError, ValueError) as exc:
            raise SemanticRetrievalUnavailable(
                "semantic retrieval unavailable: embedding provider returned invalid data"
            ) from exc
        if not values or not all(math.isfinite(item) for item in values):
            raise SemanticRetrievalUnavailable(
                "semantic retrieval unavailable: embedding provider returned an invalid vector"
            )
        return values

    async def _by_semantic(
        self,
        query: str,
        scope: MemoryScope | None,
        scope_id: str | None,
        limit: int,
        tags: Sequence[str] | None = None,
    ) -> list[MemoryRecord]:
        await self._store.ensure_embeddings(scope, scope_id, tags=tags)
        vector = await self._query_embedding(query)
        candidates = await self._store.retrieve_by_embedding(
            vector, scope, scope_id, limit * 8, tags=tags
        )
        return [
            record
            for _, record in sorted(
                (
                    (_quality_score(record, index=index, base=score), record)
                    for index, (record, score) in enumerate(candidates)
                ),
                key=lambda item: (item[0], item[1].created_at, item[1].id),
                reverse=True,
            )[:limit]
        ]

    async def _by_semantic_scopes(
        self,
        query: str,
        scopes: Sequence[tuple[MemoryScope, str | None]],
        limit: int,
        tags: Sequence[str] | None = None,
    ) -> list[MemoryRecord]:
        for scope, scope_id in scopes:
            await self._store.ensure_embeddings(scope, scope_id, tags=tags)
        vector = await self._query_embedding(query)
        candidates = await self._store.retrieve_by_embedding_scopes(
            vector, scopes, limit * 8, tags=tags
        )
        return [record for record, _ in _rank_vector(candidates, limit)]

    async def _by_hybrid(
        self,
        query: str,
        scope: MemoryScope | None,
        scope_id: str | None,
        limit: int,
        tags: Sequence[str] | None = None,
    ) -> list[MemoryRecord]:
        await self._store.ensure_embeddings(scope, scope_id, tags=tags)
        vector = await self._query_embedding(query)
        lexical = await self._store.retrieve_by_fts(query, scope, scope_id, limit * 8, tags=tags)
        vectors = await self._store.retrieve_by_embedding(
            vector, scope, scope_id, limit * 8, tags=tags
        )
        return _fuse(lexical, [record for record, _ in vectors], limit)

    async def _by_hybrid_scopes(
        self,
        query: str,
        scopes: Sequence[tuple[MemoryScope, str | None]],
        limit: int,
        tags: Sequence[str] | None = None,
    ) -> list[MemoryRecord]:
        for scope, scope_id in scopes:
            await self._store.ensure_embeddings(scope, scope_id, tags=tags)
        vector = await self._query_embedding(query)
        lexical = await self._store.retrieve_by_fts_scopes(query, scopes, limit * 8, tags=tags)
        vectors = await self._store.retrieve_by_embedding_scopes(
            vector, scopes, limit * 8, tags=tags
        )
        return _fuse(lexical, [record for record, _ in vectors], limit)


def _rank_vector(
    candidates: list[tuple[MemoryRecord, float]], limit: int
) -> list[tuple[MemoryRecord, float]]:
    scored = [
        (
            _quality_score(record, index=index, base=score),
            record,
        )
        for index, (record, score) in enumerate(candidates)
    ]
    scored.sort(key=lambda item: (item[0], item[1].created_at, item[1].id), reverse=True)
    return [(record, score) for score, record in scored[:limit]]


def _fuse(
    lexical: list[MemoryRecord], vector: list[MemoryRecord], limit: int
) -> list[MemoryRecord]:
    """Deterministic reciprocal-rank fusion with explicit quality signals."""
    records: dict[str, MemoryRecord] = {record.id: record for record in (*lexical, *vector)}
    scores: dict[str, float] = {record_id: 0.0 for record_id in records}
    for rank, record in enumerate(lexical, 1):
        scores[record.id] += 1.0 / (60.0 + rank)
    for rank, record in enumerate(vector, 1):
        scores[record.id] += 1.0 / (60.0 + rank)
    recent = sorted(records.values(), key=lambda item: item.created_at, reverse=True)
    recent_rank = {record.id: index for index, record in enumerate(recent)}
    for record in records.values():
        scores[record.id] += _quality_score(record, index=recent_rank[record.id]) * 0.01
    return [
        records[record_id]
        for record_id, _ in sorted(
            records.items(),
            key=lambda item: (scores[item[0]], records[item[0]].created_at, item[0]),
            reverse=True,
        )[:limit]
    ]


def _quality_score(record: MemoryRecord, *, index: int, base: float = 0.0) -> float:
    trust = {
        "authority": 1.0,
        "configured_instruction": 0.8,
        "user_content": 0.7,
        "agent_curated": 0.5,
        "external_content": 0.25,
        "untrusted": 0.0,
    }.get(record.trust.value, 0.0)
    scope = {"session": 1.0, "task": 0.95, "project": 0.85, "user": 0.75, "global": 0.65}.get(
        record.scope.value, 0.0
    )
    confidence = max(0.0, min(1.0, float(record.confidence or 0.5)))
    recency = 1.0 / (1.0 + max(0, index))
    return float(base) + scope * 0.25 + trust * 0.2 + confidence * 0.15 + recency * 0.1


__all__ = ["MemoryRetriever"]
