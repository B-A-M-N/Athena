"""SQL retrieval mechanics beneath :class:`athena.memory.store.MemoryStore`.

The store remains the authority for record identity, metadata, trust, and
conflict state. This module owns only bounded scope predicates and SQLite FTS
queries used by lexical and recency retrieval.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from athena.protocol.memory import MemoryRecord, MemoryScope
from athena.state.database import Database

__all__ = ["MemoryRetrievalStore"]


class MemoryRetrievalStore:
    """Execute normalized lexical/recency memory queries."""

    def __init__(
        self,
        db: Database,
        *,
        record_from_row: Callable[[Mapping[str, Any]], MemoryRecord],
    ) -> None:
        self._db = db
        self._record_from_row = record_from_row

    async def scope_where(
        self,
        scope: MemoryScope | None,
        scope_id: str | None,
        tags: Sequence[str] | None = None,
        *,
        include_inactive: bool = False,
        include_conflicts: bool = False,
    ) -> tuple[str, list[Any]]:
        """Build the canonical current-state scope predicate."""
        conds: list[str] = []
        params: list[Any] = []
        if not include_inactive:
            conds.append("COALESCE(json_extract(m.metadata, '$.pending_promotion'), 0) != 1")
            conds.append(
                "(json_extract(m.metadata, '$._athena:valid_from') IS NULL "
                "OR datetime(json_extract(m.metadata, '$._athena:valid_from')) <= datetime('now'))"
            )
            conds.append(
                "(json_extract(m.metadata, '$._athena:valid_until') IS NULL "
                "OR datetime(json_extract(m.metadata, '$._athena:valid_until')) >= datetime('now'))"
            )
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
        return [self._record_from_row(row) for row in rows]

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
        scope_w, params = await self.scope_where(
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
        scope_w, params = await self.scope_where(
            scope,
            scope_id,
            tags,
            include_inactive=include_inactive,
            include_conflicts=include_conflicts,
        )
        where_parts = ["memories_fts MATCH ?"]
        if scope_w:
            where_parts.append(scope_w)
        params.insert(0, match)
        params.append(limit)
        return await self._fetch_records(
            "SELECT m.* FROM memories_fts "
            "JOIN memories m ON m.rowid = memories_fts.rowid "
            f"WHERE {' AND '.join(where_parts)} ORDER BY bm25(memories_fts) LIMIT ?",
            params,
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
        state_where, state_params = await self.scope_where(
            None,
            None,
            tags,
            include_inactive=include_inactive,
            include_conflicts=include_conflicts,
        )
        where = ["memories_fts MATCH ?", state_where, "(" + " OR ".join(groups) + ")"]
        params: list[Any] = [match, *state_params, *group_params, limit]
        return await self._fetch_records(
            "SELECT m.* FROM memories_fts "
            "JOIN memories m ON m.rowid = memories_fts.rowid "
            "WHERE " + " AND ".join(where) + " ORDER BY bm25(memories_fts) LIMIT ?",
            params,
        )

    @staticmethod
    def sanitize_match(query: str) -> str:
        """Reduce free text to a safe FTS5 OR-expression of quoted barewords."""
        tokens = re.findall(r"[a-z0-9_']+", query.lower())
        seen: list[str] = []
        for token in tokens:
            if token not in seen:
                seen.append(token)
        return " OR ".join(f'"{token}"' for token in seen[:32])
