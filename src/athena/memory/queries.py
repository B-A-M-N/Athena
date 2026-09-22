"""Read-only durable memory-record projections."""

from __future__ import annotations

from typing import Any

from athena.memory.record_codec import SCOPE_ID_KEY, row_to_record
from athena.protocol.memory import MemoryKind, MemoryRecord, MemoryScope
from athena.state.database import Database


class MemoryRecordQuery:
    """Project persisted memory rows without owning memory mutations."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, id: str) -> MemoryRecord | None:
        row = await self._db.fetch_one("SELECT * FROM memories WHERE id = ?", (id,))
        if row is None:
            return None
        return row_to_record(row)

    async def list_by_scope(
        self,
        scope: MemoryScope,
        scope_id: str | None,
    ) -> list[MemoryRecord]:
        conditions = ["scope = ?"]
        params: list[Any] = [scope.value]
        if scope_id:
            conditions.append(f"json_extract(metadata, '$.{SCOPE_ID_KEY}') = ?")
            params.append(scope_id)
        sql = f"SELECT * FROM memories WHERE {' AND '.join(conditions)} ORDER BY created_at DESC"
        rows = await self._db.fetch_all(sql, params)
        return [row_to_record(row) for row in rows]

    async def list_by_kind(self, kind: MemoryKind) -> list[MemoryRecord]:
        rows = await self._db.fetch_all(
            "SELECT * FROM memories WHERE kind = ? ORDER BY created_at DESC",
            (kind.value,),
        )
        return [row_to_record(row) for row in rows]

    async def count(self) -> int:
        row = await self._db.fetch_one("SELECT COUNT(*) AS c FROM memories")
        return int(row["c"]) if row else 0


__all__ = ["MemoryRecordQuery"]
