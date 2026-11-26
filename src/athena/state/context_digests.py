"""Durable storage for the neutral context-digest value object."""

from __future__ import annotations

import json

from athena.protocol.context import ContextDigest, row_to_digest
from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from athena.state.database import Database


class ContextDigestStore:
    """SQLite-backed digest history with principal/session isolation."""

    def __init__(self, db: Database, *, retention_per_task: int = 16) -> None:
        self._db = db
        self._retention_per_task = max(2, int(retention_per_task))

    async def save(self, digest: ContextDigest) -> ContextDigest:
        now = digest.updated_at or utcnow().isoformat()
        created = digest.created_at or now
        digest_id = digest.id or new_id("digest")
        await self._db.execute(
            "INSERT INTO context_digests("
            "id, task_id, session_id, principal_id, level, fields, "
            "transcript_anchors, recovery_queries, created_at, updated_at, "
            "parent_digest_id, source_digest_ids, range_start_message_id, range_end_message_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                digest_id,
                digest.task_id,
                digest.session_id,
                digest.principal_id,
                max(0, int(digest.level)),
                json.dumps(digest.normalized_fields(), sort_keys=True, default=str),
                json.dumps(list(digest.transcript_anchors), sort_keys=True),
                json.dumps(list(digest.recovery_queries), sort_keys=True),
                created,
                now,
                digest.parent_digest_id,
                json.dumps(list(digest.source_digest_ids), sort_keys=True),
                digest.range_start_message_id,
                digest.range_end_message_id,
            ),
        )
        await self._db.execute(
            "DELETE FROM context_digests WHERE task_id = ? AND principal_id = ? "
            "AND id NOT IN (SELECT id FROM context_digests WHERE task_id = ? "
            "AND principal_id = ? ORDER BY updated_at DESC LIMIT ?)",
            (
                digest.task_id,
                digest.principal_id,
                digest.task_id,
                digest.principal_id,
                self._retention_per_task,
            ),
        )
        return ContextDigest(
            id=digest_id,
            task_id=digest.task_id,
            session_id=digest.session_id,
            principal_id=digest.principal_id,
            level=digest.level,
            parent_digest_id=digest.parent_digest_id,
            source_digest_ids=digest.source_digest_ids,
            range_start_message_id=digest.range_start_message_id,
            range_end_message_id=digest.range_end_message_id,
            fields=digest.normalized_fields(),
            transcript_anchors=digest.transcript_anchors,
            recovery_queries=digest.recovery_queries,
            created_at=created,
            updated_at=now,
        )

    async def latest_for_task(self, task_id: str, principal_id: str) -> ContextDigest | None:
        row = await self._db.fetch_one(
            "SELECT * FROM context_digests WHERE task_id = ? AND principal_id = ? "
            "ORDER BY level DESC, updated_at DESC LIMIT 1",
            (task_id, principal_id),
        )
        return row_to_digest(row) if row else None

    async def list_for_session(
        self, session_id: str, principal_id: str, *, limit: int = 3
    ) -> list[ContextDigest]:
        rows = await self._db.fetch_all(
            "SELECT * FROM context_digests WHERE session_id = ? AND principal_id = ? "
            "ORDER BY updated_at DESC, level DESC LIMIT ?",
            (session_id, principal_id, max(1, min(int(limit), 20))),
        )
        return [row_to_digest(row) for row in rows]

    async def latest_for_session(self, session_id: str, principal_id: str) -> ContextDigest | None:
        row = await self._db.fetch_one(
            "SELECT * FROM context_digests WHERE session_id = ? AND principal_id = ? "
            "ORDER BY level DESC, updated_at DESC LIMIT 1",
            (session_id, principal_id),
        )
        return row_to_digest(row) if row else None


__all__ = ["ContextDigestStore"]
