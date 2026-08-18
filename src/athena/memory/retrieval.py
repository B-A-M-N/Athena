from __future__ import annotations

import re
from typing import TYPE_CHECKING, Sequence

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

    SEMANTIC is best-effort: there is no embedding infrastructure wired in the
    storage layer, so it first relies on FTS5 ranking (``bm25``) and then
    re-ranks by normalized keyword overlap between the query and each
    candidate's ``content + summary``. When an embedding backend is added later
    this path should switch to cosine similarity; the keyword-overlap fallback
    is intentionally conservative and documented here.

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
            return await self._store.retrieve_by_recency(
                scope, scope_id, limit, tags=tags)
        if mode is RetrievalMode.EXACT:
            return await self._store.retrieve_by_fts(
                query, scope, scope_id, limit, tags=tags)
        return await self._by_semantic(query, scope, scope_id, limit, tags=tags)

    async def retrieve_all(
        self, query: str, limit: int = 10
    ) -> list[MemoryRecord]:
        return await self._by_semantic(query, None, None, limit)

    async def _by_semantic(
        self, query: str, scope: MemoryScope | None, scope_id: str | None,
        limit: int, tags: Sequence[str] | None = None,
    ) -> list[MemoryRecord]:
        candidate = await self._store.retrieve_by_fts(
            query, scope, scope_id, limit * 8, tags=tags)
        qset = _tokens(query)
        if not qset:
            return candidate[:limit]
        scored: list[tuple[float, MemoryRecord]] = []
        for rec in candidate:
            text = " ".join((rec.content or "", rec.summary or "")).lower()
            intersect = len(qset & _tokens(text))
            score = intersect / len(qset)
            scored.append((score, rec))
        scored.sort(key=lambda t: t[0], reverse=True)
        best = [rec for score, rec in scored if score > 0]
        return best[:limit]


__all__ = ["MemoryRetriever"]