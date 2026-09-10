from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from athena.protocol.messages import Message, Provenance, Role, SourceType
from athena.state.database import Database
from athena.state.sessions import (
    _deserialize_block,
    _deserialize_provenance,
    _extract_text,
    _serialize_block,
    _serialize_provenance,
)


class MessageStore:
    """Append-only message transcript (BHV-025, BHV-028).

    Messages are immutable historical records. This store never mutates or
    deletes an existing row; it only appends.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def append(self, message: Message) -> None:
        session_id: str | None = None
        if message.metadata:
            raw = message.metadata.get("session_id")
            if isinstance(raw, str):
                session_id = raw
        if session_id is None:
            raise ValueError("append requires a session_id in message metadata")
        await self.append_to_session(session_id, message)

    async def append_to_session(
        self,
        session_id: str,
        message: Message,
    ) -> None:
        if session_id is None:
            raise ValueError("append_to_session requires a session_id")
        blocks_json = json.dumps([_serialize_block(b) for b in message.blocks])
        prov_json = json.dumps(_serialize_provenance(message.provenance))
        meta_json = json.dumps(dict(message.metadata))
        text_content = _extract_text(message.blocks)
        await self._db.execute(
            "INSERT INTO messages(id, session_id, role, blocks, text_content, "
            "created_at, provenance, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message.id,
                session_id,
                message.role.value,
                blocks_json,
                text_content,
                message.created_at.isoformat(),
                prov_json,
                meta_json,
            ),
        )

    async def append_idempotent(self, message: Message) -> bool:
        """Append one durable message, returning whether this call inserted it.

        This is the persistence boundary for replayed model responses.  A
        retry after a process restart must not depend on an in-memory set or
        manufacture a second transcript row.  A reused message identity in a
        different session is a hard collision rather than a silent no-op.
        """
        session_id = message.metadata.get("session_id") if message.metadata else None
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("append_idempotent requires a session_id in message metadata")
        existing = await self._db.fetch_one(
            "SELECT session_id FROM messages WHERE id = ?",
            (message.id,),
        )
        if existing is not None:
            if str(existing.get("session_id") or "") != session_id:
                raise ValueError(f"message id already belongs to another session: {message.id}")
            return False
        blocks_json = json.dumps([_serialize_block(b) for b in message.blocks])
        prov_json = json.dumps(_serialize_provenance(message.provenance))
        meta_json = json.dumps(dict(message.metadata))
        text_content = _extract_text(message.blocks)
        cursor = await self._db.execute(
            "INSERT OR IGNORE INTO messages(id, session_id, role, blocks, text_content, "
            "created_at, provenance, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message.id,
                session_id,
                message.role.value,
                blocks_json,
                text_content,
                message.created_at.isoformat(),
                prov_json,
                meta_json,
            ),
        )
        return cursor.rowcount == 1

    async def append_user_turn(self, session_id: str, message: Message) -> bool:
        """Append a canonical user turn unless its task association exists."""
        # The service assigns the stable ``msg_user_<task-id>`` identity.
        # Check that primary key directly; scanning and decoding every user
        # row makes retry cost grow with the whole session and can race with
        # concurrent submissions.
        existing = await self._db.fetch_one(
            "SELECT id FROM messages WHERE id = ?",
            (message.id,),
        )
        if existing is not None:
            return False
        blocks_json = json.dumps([_serialize_block(b) for b in message.blocks])
        prov_json = json.dumps(_serialize_provenance(message.provenance))
        meta_json = json.dumps(dict(message.metadata))
        text_content = _extract_text(message.blocks)
        cursor = await self._db.execute(
            "INSERT OR IGNORE INTO messages(id, session_id, role, blocks, text_content, "
            "created_at, provenance, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message.id,
                session_id,
                message.role.value,
                blocks_json,
                text_content,
                message.created_at.isoformat(),
                prov_json,
                meta_json,
            ),
        )
        return cursor.rowcount == 1

    async def list_session_messages(
        self,
        session_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Message]:
        rows = await self._db.fetch_all(
            "SELECT * FROM messages WHERE session_id = ? "
            "ORDER BY created_at ASC, rowid ASC LIMIT ? OFFSET ?",
            (session_id, limit, offset),
        )
        return [_row_to_message(r) for r in rows]

    async def list_recent_session_messages(
        self,
        session_id: str,
        limit: int = 100,
    ) -> list[Message]:
        """Return the newest messages in chronological order.

        The inner query applies the limit before reversing the rows. This is
        the bounded tail the kernel needs for current-turn context, rather
        than the oldest page returned by ``list_session_messages``.
        """
        rows = await self._db.fetch_all(
            "SELECT * FROM ("
            "SELECT rowid AS _message_rowid, * FROM messages WHERE session_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?"
            ") ORDER BY created_at ASC, _message_rowid ASC",
            (session_id, limit),
        )
        return [_row_to_message(r) for r in rows]

    async def list_causal_messages(
        self,
        session_id: str,
        task_id: str,
        limit: int = 100,
    ) -> list[Message]:
        """Return the session history causally visible to ``task_id``.

        A task sees the history before its canonical user turn and its own
        subsequent messages.  A later canonical user turn is a hard boundary,
        so concurrently submitted work cannot leak into an earlier task just
        because both tasks share a session.
        """
        rows = await self._db.fetch_all(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC, rowid ASC",
            (session_id,),
        )
        messages = [_row_to_message(row) for row in rows]
        canonical_id = f"msg_user_{task_id}"
        start = next(
            (
                index
                for index, message in enumerate(messages)
                if message.id == canonical_id
                or (_is_canonical_user_turn(message) and _belongs_to_task(message, task_id))
            ),
            None,
        )
        if start is None:
            return messages[-limit:] if limit > 0 else []

        bounded: list[Message] = []
        for index, message in enumerate(messages):
            if (
                index > start
                and _is_canonical_user_turn(message)
                and not _belongs_to_task(message, task_id)
            ):
                break
            bounded.append(message)
        return bounded[-limit:] if limit > 0 else []

    async def list_task_messages(
        self,
        session_id: str,
        task_id: str,
        limit: int | None = None,
    ) -> list[Message]:
        """Return the tail beginning at a task's canonical user turn.

        Assistant/result rows are intentionally associated by the durable
        canonical marker rather than by a copied task id. The knowledge
        observer can then stop at the next canonical marker without loading
        the oldest arbitrary session page.
        """
        canonical_id = f"msg_user_{task_id}"
        task_limit_sql = " LIMIT ?" if limit is not None else ""
        task_params: tuple[Any, ...] = (session_id, canonical_id, task_id)
        if limit is not None:
            task_params += (limit,)
        task_rows = await self._db.fetch_all(
            "SELECT * FROM messages WHERE session_id = ? "
            "AND (id = ? OR json_extract(metadata, '$.task_id') = ?) "
            "ORDER BY created_at ASC, rowid ASC" + task_limit_sql,
            task_params,
        )
        if len(task_rows) > 1:
            return [_row_to_message(r) for r in task_rows]
        bound = await self._db.fetch_one(
            "SELECT created_at, id FROM messages WHERE id = ? AND session_id = ?",
            (canonical_id, session_id),
        )
        if bound is None:
            return await self.list_recent_session_messages(session_id, limit or 100)
        limit_sql = " LIMIT ?" if limit is not None else ""
        created_at = str(bound["created_at"])
        params: tuple[Any, ...] = (session_id, created_at)
        if limit is not None:
            params += (limit,)
        rows = await self._db.fetch_all(
            "SELECT * FROM messages WHERE session_id = ? "
            "AND created_at >= ? "
            "ORDER BY created_at ASC, rowid ASC" + limit_sql,
            params,
        )
        messages = [_row_to_message(r) for r in rows]
        # Legacy rows may lack task metadata.  The next canonical user marker
        # is the durable causal boundary; never let a later same-session turn
        # leak into the current task merely because its timestamp is close.
        bounded: list[Message] = []
        for message in messages:
            if (
                bounded
                and _is_canonical_user_turn(message)
                and not _belongs_to_task(message, task_id)
            ):
                break
            bounded.append(message)
        return bounded

    async def count_session_messages(self, session_id: str) -> int:
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?",
            (session_id,),
        )
        return int((row or {}).get("n") or 0)

    async def search(
        self,
        query: str,
        *,
        principal_id: str | None = None,
        project_id: str | None = None,
        session_ids: tuple[str, ...] | list[str] | None = None,
        limit: int = 20,
        context_window: int = 0,
    ) -> list[dict]:
        """FTS5 search over historical conversation text.

        Scope is explicit and closed: either the caller names session ids or
        the host supplies a principal ownership filter. There is no
        cross-principal or unbounded-global mode. Each hit
        carries provenance (session, role, timestamp, message id) and, on
        request, a bounded chronological context window read from the same
        session. Matches are ordered by bm25 relevance, newest first on ties.
        """
        match = sanitize_fts_query(query)
        if not match or limit <= 0 or (not session_ids and not principal_id):
            return []
        predicates = ["messages_fts MATCH ?"]
        params: list[Any] = [match]
        if session_ids:
            placeholders = ", ".join("?" for _ in session_ids)
            predicates.append(f"messages.session_id IN ({placeholders})")
            params.extend(session_ids)
        if principal_id:
            predicates.append(
                "COALESCE(sessions.principal_id, json_extract(sessions.metadata, '$.principal_id')) = ?"
            )
            params.append(principal_id)
        if project_id:
            predicates.append(
                "COALESCE(sessions.project_id, json_extract(sessions.metadata, '$.project_id')) = ?"
            )
            params.append(project_id)
        params.append(limit)
        rows = await self._db.fetch_all(
            "SELECT messages.*, messages.rowid AS _rowid, bm25(messages_fts) AS _rank "
            "FROM messages JOIN messages_fts ON messages_fts.rowid = messages.rowid "
            "JOIN sessions ON sessions.id = messages.session_id "
            "WHERE " + " AND ".join(predicates) + " "
            "ORDER BY _rank ASC, messages.created_at DESC, messages.rowid DESC "
            "LIMIT ?",
            tuple(params),
        )
        results = [_hit_to_record(row) for row in rows]
        for result, row in zip(results, rows, strict=True):
            result["_rowid"] = row.get("_rowid")
        if context_window > 0:
            for hit in results:
                hit["context"] = await self._context_around_hit(hit, context_window)
        for hit in results:
            hit.pop("_rowid", None)
        return results

    async def read_context(
        self,
        anchor: str,
        *,
        session_id: str | None = None,
        principal_id: str | None = None,
        project_id: str | None = None,
        before: int = 3,
        after: int = 3,
    ) -> dict | None:
        """Read bounded detail around an anchor after host-side ownership checks."""
        if not anchor or not (session_id or principal_id) or before < 0 or after < 0:
            return None
        predicates = ["messages.id = ?"]
        params: list[Any] = [anchor]
        if session_id:
            predicates.append("messages.session_id = ?")
            params.append(session_id)
        if principal_id:
            predicates.append(
                "COALESCE(sessions.principal_id, json_extract(sessions.metadata, '$.principal_id')) = ?"
            )
            params.append(principal_id)
        if project_id:
            predicates.append(
                "COALESCE(sessions.project_id, json_extract(sessions.metadata, '$.project_id')) = ?"
            )
            params.append(project_id)
        row = await self._db.fetch_one(
            "SELECT messages.*, messages.rowid AS _rowid FROM messages "
            "JOIN sessions ON sessions.id = messages.session_id "
            "WHERE " + " AND ".join(predicates),
            tuple(params),
        )
        if row is None:
            return None
        hit = _hit_to_record(row)
        hit["context"] = await self._context_around_anchor(
            {**hit, "_rowid": row.get("_rowid")}, before=before, after=after
        )
        return {
            "anchor": hit,
            "session_id": row["session_id"],
            "message_id": row["id"],
            "before": before,
            "after": after,
        }

    async def _context_around_hit(self, hit: dict, context_window: int) -> list[dict]:
        """Return N messages before and after the hit from the SAME session."""
        return await self._context_around_anchor(hit, before=context_window, after=context_window)

    async def _context_around_anchor(self, hit: dict, *, before: int, after: int) -> list[dict]:
        """Return bounded messages around one owned transcript anchor."""
        session_id = hit["session_id"]
        hit_rowid = hit.get("_rowid") or hit.get("rowid")
        if hit_rowid is None:
            return [
                _context_record(message)
                for message in await self.list_recent_session_messages(
                    session_id, limit=before + after + 1
                )
            ]
        before_rows = (
            await self._db.fetch_all(
                "SELECT * FROM messages "
                "WHERE session_id = ? AND rowid <= ? "
                "ORDER BY rowid DESC LIMIT ?",
                (session_id, hit_rowid, before + 1),
            )
            if before >= 0
            else []
        )
        if before == 0:
            before_rows = before_rows[:1]
        after_rows = await self._db.fetch_all(
            "SELECT * FROM messages WHERE session_id = ? AND rowid > ? ORDER BY rowid ASC LIMIT ?",
            (session_id, hit_rowid, after),
        )
        before_messages = [_row_to_message(r) for r in reversed(before_rows)]
        after_messages = [_row_to_message(r) for r in after_rows]
        all_messages = before_messages + after_messages
        return [_context_record(m) for m in all_messages]


def sanitize_fts_query(query: str) -> str:
    """Reduce free text to a safe FTS5 OR-expression of quoted barewords.

    Mirrors the memory store's sanitizer: operators' natural language must
    never reach the FTS5 parser as syntax (quotes, NEAR, column filters,
    asterisks).
    """
    import re as _re

    tokens: list[str] = []
    for token in _re.findall(r"[a-z0-9_']+", str(query or "").lower()):
        if token not in tokens:
            tokens.append(token)
    return " OR ".join(f'"{token}"' for token in tokens[:32])


def _hit_to_record(row: dict | Any) -> dict:
    return {
        "message_id": row["id"],
        "session_id": row["session_id"],
        "role": row["role"],
        "text": row["text_content"] or "",
        "created_at": row["created_at"],
        "task_id": _task_id_of(row),
        "rank": float(row["_rank"]) if row.get("_rank") is not None else None,
    }


def _context_record(message: Message) -> dict:
    return {
        "message_id": message.id,
        "role": message.role.value,
        "text": _extract_text(message.blocks),
        "created_at": message.created_at.isoformat(),
    }


def _task_id_of(row: dict | Any) -> str | None:
    import json as _json

    raw = row.get("metadata")
    if not raw:
        return None
    try:
        metadata = _json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return None
    task_id = metadata.get("task_id")
    return str(task_id) if task_id else None


def _row_to_message(row: dict | Any) -> Message:
    blocks_data = json.loads(row["blocks"]) if row.get("blocks") else []
    blocks = tuple(_deserialize_block(b) for b in blocks_data)
    prov_data = json.loads(row["provenance"]) if row.get("provenance") else None
    prov = (
        _deserialize_provenance(prov_data)
        if prov_data
        else Provenance(source_type=SourceType.RUNTIME)
    )
    meta = json.loads(row["metadata"]) if row.get("metadata") else {}
    return Message(
        id=row["id"],
        role=Role(row["role"]),
        blocks=blocks,
        created_at=datetime.fromisoformat(row["created_at"]),
        provenance=prov,
        metadata=meta,
    )


def _is_canonical_user_turn(message: Message) -> bool:
    metadata = dict(message.metadata or {})
    return bool(metadata.get("canonical_user_turn")) or message.id.startswith("msg_user_")


def _belongs_to_task(message: Message, task_id: str) -> bool:
    metadata = dict(message.metadata or {})
    return str(metadata.get("task_id") or "") == str(task_id) or message.id == f"msg_user_{task_id}"


__all__ = ["MessageStore", "sanitize_fts_query"]
