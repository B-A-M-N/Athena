"""Durable ownership records for external specialist sessions."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

from athena.delegates.models import DelegateSession
from athena.protocol.messages import utcnow
from athena.state.database import Database


class DelegateSessionStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def save(self, session: DelegateSession) -> None:
        await self._db.execute(
            "INSERT INTO delegate_sessions (id, delegate_id, task_id, session_id, "
            "remote_session_id, workspace_root, state, created_at, last_seen_at, "
            "launch_signature, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET remote_session_id=excluded.remote_session_id, "
            "state=excluded.state, last_seen_at=excluded.last_seen_at, "
            "metadata=excluded.metadata",
            (
                session.id,
                session.delegate_id,
                session.task_id,
                session.session_id,
                session.remote_session_id,
                session.workspace_root,
                session.state,
                _dt(session.created_at),
                _dt(session.last_seen_at),
                session.launch_signature,
                json.dumps(dict(session.metadata), sort_keys=True),
            ),
        )

    async def get(self, session_id: str, *, task_id: str | None = None) -> DelegateSession | None:
        sql = "SELECT * FROM delegate_sessions WHERE id = ?"
        params: list[Any] = [session_id]
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        row = await self._db.fetch_one(sql, params)
        return _from_row(row) if row else None

    async def list(self, *, task_id: str | None = None) -> list[DelegateSession]:
        if task_id is None:
            rows = await self._db.fetch_all(
                "SELECT * FROM delegate_sessions ORDER BY last_seen_at DESC"
            )
        else:
            rows = await self._db.fetch_all(
                "SELECT * FROM delegate_sessions WHERE task_id = ? ORDER BY last_seen_at DESC",
                (task_id,),
            )
        return [_from_row(row) for row in rows]

    async def update_state(
        self, session_id: str, state: str, *, task_id: str
    ) -> DelegateSession | None:
        session = await self.get(session_id, task_id=task_id)
        if session is None:
            return None
        updated = DelegateSession(
            **{
                **session.__dict__,
                "state": state,
                "last_seen_at": utcnow(),
            }
        )
        await self.save(updated)
        return updated


def _dt(value: datetime | None) -> str:
    return (value or utcnow()).isoformat()


def _from_row(row: Mapping[str, Any]) -> DelegateSession:
    return DelegateSession(
        id=str(row["id"]),
        delegate_id=str(row["delegate_id"]),
        task_id=str(row["task_id"]),
        session_id=row.get("session_id"),
        remote_session_id=row.get("remote_session_id"),
        workspace_root=str(row.get("workspace_root") or ""),
        state=str(row.get("state") or "unknown"),
        created_at=_parse(row.get("created_at")),
        last_seen_at=_parse(row.get("last_seen_at")),
        launch_signature=str(row.get("launch_signature") or ""),
        metadata=_json(row.get("metadata")),
    )


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _json(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
        return dict(parsed or {})
    except (TypeError, ValueError):
        return {}


__all__ = ["DelegateSessionStore"]
