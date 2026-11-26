"""Derived embedding-index mechanics subordinate to the canonical memory store."""

from __future__ import annotations

import inspect
import json
import logging
import math
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Mapping

from athena.protocol.memory import MemoryRecord, MemoryScope
from athena.protocol.messages import utcnow
from athena.state.database import Database

_logger = logging.getLogger("athena.memory.embedding_index")


class MemoryEmbeddingIndex:
    """Own optional vector indexing while leaving memory truth in ``MemoryStore``."""

    def __init__(
        self,
        *,
        db: Database,
        provider: Any,
        scope_where: Callable[..., Awaitable[tuple[str, list[Any]]]],
        record_from_row: Callable[[Mapping[str, Any]], MemoryRecord],
    ) -> None:
        self._db = db
        self._provider = provider
        self._scope_where = scope_where
        self._record_from_row = record_from_row

    async def index(self, record: MemoryRecord) -> bool:
        provider = self._provider
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
        except Exception as exc:  # noqa: BLE001 - derived index failures must not lose memory truth
            _logger.warning("memory embedding index failed for %s: %s", record.id, exc)
            return False

    async def ensure(
        self,
        scope: MemoryScope | None = None,
        scope_id: str | None = None,
        *,
        tags: Sequence[str] | None = None,
        limit: int = 5000,
        include_inactive: bool = False,
        include_conflicts: bool = False,
    ) -> int:
        """Backfill missing or stale vectors only when semantic retrieval asks."""
        provider = self._provider
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
            record = self._record_from_row(row)
            if (
                row.get("embedding_content_hash") == memory_content_hash(record)
                and row.get("embedding_model") == model
                and row.get("embedding_version") == version
            ):
                continue
            if await self.index(record):
                indexed += 1
        return indexed

    async def retrieve(
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
        scope_where, params = await self._scope_where(
            scope,
            scope_id,
            tags,
            include_inactive=include_inactive,
            include_conflicts=include_conflicts,
        )
        where = f"WHERE {scope_where}" if scope_where else ""
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
            scored.append((self._record_from_row(row), score))
        scored.sort(key=lambda item: (item[1], item[0].created_at, item[0].id), reverse=True)
        return scored[: max(1, int(limit))]

    async def retrieve_scopes(
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
                await self.retrieve(
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


def memory_content_hash(record: MemoryRecord) -> str:
    """Hash the exact canonical text represented by a stored vector."""
    import hashlib

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


__all__ = ["MemoryEmbeddingIndex", "memory_content_hash"]
