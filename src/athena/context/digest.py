"""Durable hierarchical context digests.

Digests are compact working-state records, not replacements for the immutable
transcript. They make compaction recoverable by retaining structured state and
anchors/queries that can be used to fetch the omitted detail later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from athena.state.database import Database


_FIELD_NAMES = (
    "objective",
    "constraints",
    "decisions",
    "completed_work",
    "pending_work",
    "files_resources",
    "runtime_state",
    "child_tasks",
    "evidence",
    "artifacts",
    "failures",
    "open_questions",
    "acceptance_state",
)
_MAX_FIELD_ITEMS = 8
_MAX_FIELD_TEXT = 600


@dataclass(frozen=True)
class ContextDigest:
    task_id: str
    session_id: str | None
    principal_id: str
    level: int
    parent_digest_id: str | None = None
    source_digest_ids: tuple[str, ...] = ()
    range_start_message_id: str | None = None
    range_end_message_id: str | None = None
    fields: Mapping[str, Any] = field(default_factory=dict)
    transcript_anchors: tuple[str, ...] = ()
    recovery_queries: tuple[str, ...] = ()
    id: str = ""
    created_at: str = ""
    updated_at: str = ""

    def normalized_fields(self) -> dict[str, Any]:
        result = {name: _bound_field(self.fields.get(name, []), name=name) for name in _FIELD_NAMES}
        result["objective"] = str(result["objective"] or "")
        return result

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "principal_id": self.principal_id,
            "level": self.level,
            "parent_digest_id": self.parent_digest_id,
            "source_digest_ids": list(self.source_digest_ids),
            "range_start_message_id": self.range_start_message_id,
            "range_end_message_id": self.range_end_message_id,
            "fields": self.normalized_fields(),
            "transcript_anchors": list(self.transcript_anchors),
            "recovery_queries": list(self.recovery_queries),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


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
        return _row_to_digest(row) if row else None

    async def list_for_session(
        self, session_id: str, principal_id: str, *, limit: int = 3
    ) -> list[ContextDigest]:
        rows = await self._db.fetch_all(
            "SELECT * FROM context_digests WHERE session_id = ? AND principal_id = ? "
            "ORDER BY updated_at DESC, level DESC LIMIT ?",
            (session_id, principal_id, max(1, min(int(limit), 20))),
        )
        return [_row_to_digest(row) for row in rows]

    async def latest_for_session(self, session_id: str, principal_id: str) -> ContextDigest | None:
        row = await self._db.fetch_one(
            "SELECT * FROM context_digests WHERE session_id = ? AND principal_id = ? "
            "ORDER BY level DESC, updated_at DESC LIMIT 1",
            (session_id, principal_id),
        )
        return _row_to_digest(row) if row else None


def _json_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return ()
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return ()
    return tuple(str(item) for item in value if item)


def _row_to_digest(row: Mapping[str, Any]) -> ContextDigest:
    try:
        fields = json.loads(row.get("fields") or "{}")
    except (TypeError, ValueError):
        fields = {}
    if not isinstance(fields, dict):
        fields = {}
    return ContextDigest(
        id=str(row.get("id") or ""),
        task_id=str(row.get("task_id") or ""),
        session_id=str(row.get("session_id")) if row.get("session_id") else None,
        principal_id=str(row.get("principal_id") or ""),
        level=int(row.get("level") or 0),
        parent_digest_id=(
            str(row.get("parent_digest_id")) if row.get("parent_digest_id") else None
        ),
        source_digest_ids=_json_list(row.get("source_digest_ids")),
        range_start_message_id=(
            str(row.get("range_start_message_id")) if row.get("range_start_message_id") else None
        ),
        range_end_message_id=(
            str(row.get("range_end_message_id")) if row.get("range_end_message_id") else None
        ),
        fields=fields,
        transcript_anchors=_json_list(row.get("transcript_anchors")),
        recovery_queries=_json_list(row.get("recovery_queries")),
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
    )


def _bound_field(value: Any, *, name: str) -> Any:
    """Keep the active digest compact while retaining typed state."""
    if name == "objective":
        return str(value or "")[:1200]
    if isinstance(value, Mapping):
        return {
            str(key): _bound_field(item, name="item")
            for key, item in list(value.items())[:_MAX_FIELD_ITEMS]
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_bound_field(item, name="item") for item in list(value)[:_MAX_FIELD_ITEMS]]
    return str(value)[:_MAX_FIELD_TEXT] if value is not None else ""


__all__ = ["ContextDigest", "ContextDigestStore"]
