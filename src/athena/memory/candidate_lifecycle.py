"""Pending memory-candidate lifecycle mechanics.

This module is subordinate to :class:`athena.memory.store.MemoryStore`.
It owns the durable list/promote/discard/expiry workflow for agent-derived
memory candidates, but it does not decide trust, scope, conflict resolution,
or ordinary memory retrieval.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any

from athena.protocol.memory import MemoryRecord, MemoryScope
from athena.protocol.messages import Provenance, SourceType, utcnow
from athena.state.database import Database

__all__ = ["MemoryCandidateLifecycle"]


class MemoryCandidateLifecycle:
    """Persist the review lifecycle of pending memory candidates."""

    def __init__(
        self,
        db: Database,
        *,
        get_record: Callable[[str], Awaitable[MemoryRecord | None]],
        delete_record: Callable[[str], Awaitable[bool]],
        record_from_row: Callable[[Mapping[str, Any]], MemoryRecord],
        record_metadata: Callable[[MemoryRecord, Provenance, str], dict[str, Any]],
        on_mutation: Callable[[], None],
    ) -> None:
        self._db = db
        self._get_record = get_record
        self._delete_record = delete_record
        self._record_from_row = record_from_row
        self._record_metadata = record_metadata
        self._on_mutation = on_mutation

    async def list(self, limit: int = 100) -> list[MemoryRecord]:
        """List agent-derived records awaiting deliberate review."""
        rows = await self._db.fetch_all(
            "SELECT * FROM memories WHERE json_extract(metadata, '$.pending_promotion') = 1 "
            "ORDER BY created_at ASC LIMIT ?",
            (max(1, int(limit)),),
        )
        return [self._record_from_row(row) for row in rows]

    async def promote(
        self,
        id: str,
        *,
        scope: MemoryScope,
        scope_id: str | None = None,
    ) -> MemoryRecord | None:
        """Promote one pending candidate after an operator decision."""
        record = await self._get_record(id)
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
        from dataclasses import replace

        promoted = replace(record, scope=scope, metadata=metadata)
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
        self._on_mutation()
        return await self._get_record(id)

    async def discard(self, id: str) -> bool:
        """Discard one pending candidate, leaving ordinary records untouched."""
        record = await self._get_record(id)
        if record is None or (record.metadata or {}).get("pending_promotion") is not True:
            return False
        return await self._delete_record(id)

    async def expire(self, before: datetime) -> int:
        """Delete pending candidates older than the supplied cutoff."""
        cursor = await self._db.execute(
            "DELETE FROM memories WHERE json_extract(metadata, '$.pending_promotion') = 1 "
            "AND created_at < ?",
            (before.isoformat(),),
        )
        changed = int(cursor.rowcount or 0)
        if changed:
            self._on_mutation()
        return changed

    async def compact(self, limit: int = 512) -> int:
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
            if await self.discard(str(row["id"])):
                removed += 1
        return removed
