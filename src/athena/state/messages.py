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

    async def count_session_messages(self, session_id: str) -> int:
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?",
            (session_id,),
        )
        return int((row or {}).get("n") or 0)


def _row_to_message(row: dict | Any) -> Message:
    blocks_data = json.loads(row["blocks"]) if row.get("blocks") else []
    blocks = tuple(_deserialize_block(b) for b in blocks_data)
    prov_data = json.loads(row["provenance"]) if row.get("provenance") else None
    prov = _deserialize_provenance(prov_data) if prov_data else Provenance(source_type=SourceType.RUNTIME)
    meta = json.loads(row["metadata"]) if row.get("metadata") else {}
    return Message(
        id=row["id"],
        role=Role(row["role"]),
        blocks=blocks,
        created_at=datetime.fromisoformat(row["created_at"]),
        provenance=prov,
        metadata=meta,
    )


__all__ = ["MessageStore"]