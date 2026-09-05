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
        session_ids: tuple[str, ...] | list[str] | None = None,
        limit: int = 20,
        context_window: int = 0,
    ) -> list[dict]:
        """FTS5 search over historical conversation text.

        Scope is explicit and closed: the caller names the session ids it may
        see. There is no cross-principal or unbounded-global mode. Each hit
        carries provenance (session, role, timestamp, message id) and, on
        request, a bounded chronological context window read from the same
        session. Matches are ordered by bm25 relevance, newest first on ties.
        """
        match = sanitize_fts_query(query)
        if not match or limit <= 0 or not session_ids:
            return []
        placeholders = ", ".join("?" for _ in session_ids)
        rows = await self._db.fetch_all(
            "SELECT messages.*, bm25(messages_fts) AS _rank "
            "FROM messages JOIN messages_fts ON messages_fts.rowid = messages.rowid "
            f"WHERE messages_fts MATCH ? AND messages.session_id IN ({placeholders}) "
            "ORDER BY _rank ASC, messages.created_at DESC, messages.rowid DESC "
            "LIMIT ?",
            (match, *session_ids, limit),
        )
        results = [_hit_to_record(row) for row in rows]
        if context_window > 0:
            for hit in results:
                hit["context"] = await self._context_around_hit(hit, context_window)
        return results

    async def _context_around_hit(self, hit: dict, context_window: int) -> list[dict]:
        """Return N messages before and after the hit from the SAME session."""
        session_id = hit["session_id"]
        hit_rowid = hit.get("_rowid") or hit.get("rowid")
        if hit_rowid is None:
            return [
                _context_record(message)
                for message in await self.list_recent_session_messages(
                    session_id, limit=context_window * 2 + 1
                )
            ]
        before_rows = await self._db.fetch_all(
            "SELECT * FROM messages "
            "WHERE session_id = ? AND rowid <= ? "
            "ORDER BY rowid DESC LIMIT ?",
            (session_id, hit_rowid, context_window + 1),
        )
        after_rows = await self._db.fetch_all(
            "SELECT * FROM messages WHERE session_id = ? AND rowid > ? ORDER BY rowid ASC LIMIT ?",
            (session_id, hit_rowid, context_window),
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
