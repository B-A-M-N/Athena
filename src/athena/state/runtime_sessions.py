from __future__ import annotations

import json

from athena.protocol.messages import utcnow
from athena.state.database import Database

__all__ = ["RuntimeSessionStore"]


class RuntimeSessionStore:
    """Persistent record of runtime sessions (P0-22).

    Backs the ``runtime_sessions`` table so crash recovery (RecoveryManager)
    can tell the truth about which sessions were owned by Athena vs lost on
    restart. ``is_alive`` encodes session state: ``1`` == active, ``0`` ==
    closed. ``backend`` identifies the runtime (shell / python / node / ...).
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def start(
        self,
        session_id: str,
        *,
        task_id: str,
        backend: str,
        runtime: str | None = None,
        cwd: str | None = None,
        pid: int | None = None,
        metadata: dict | None = None,
    ) -> None:
        now = utcnow().isoformat()
        await self._db.execute(
            "INSERT INTO runtime_sessions("
            "id, task_id, backend, pid, is_alive, started_at, last_heartbeat, ended_at, metadata"
            ") VALUES (?, ?, ?, ?, 1, ?, ?, NULL, ?)",
            (
                session_id,
                task_id,
                backend,
                pid,
                now,
                now,
                json.dumps(
                    {
                        **dict(metadata or {}),
                        **({"runtime": runtime} if runtime else {}),
                        **({"cwd": cwd} if cwd else {}),
                    }
                ),
            ),
        )

    async def mark_closed(self, session_id: str, *, metadata: dict | None = None) -> None:
        extra = json.dumps(metadata) if metadata else None
        if extra:
            values: tuple[str, ...] = (utcnow().isoformat(), extra)
            columns = "is_alive = 0, ended_at = ?, metadata = ?,"
        else:
            values = (utcnow().isoformat(),)
            columns = "is_alive = 0, ended_at = ?,"
        await self._db.execute(
            f"UPDATE runtime_sessions SET {columns} last_heartbeat = ? WHERE id = ?",
            (*values, utcnow().isoformat(), session_id),
        )

    async def set_alive(self, session_id: str, alive: bool) -> None:
        flag = 1 if alive else 0
        await self._db.execute(
            "UPDATE runtime_sessions SET is_alive = ?, "
            "ended_at = CASE WHEN ? = 1 THEN NULL ELSE ended_at END, "
            "last_heartbeat = CASE WHEN ? = 1 THEN ? ELSE last_heartbeat END "
            "WHERE id = ?",
            (flag, flag, flag, utcnow().isoformat(), session_id),
        )

    async def get(self, session_id: str) -> dict | None:
        row = await self._db.fetch_one("SELECT * FROM runtime_sessions WHERE id = ?", (session_id,))
        if row is None:
            return None
        return _decode(row)

    async def list_for_task(self, task_id: str) -> list[dict]:
        rows = await self._db.fetch_all(
            "SELECT * FROM runtime_sessions WHERE task_id = ? ORDER BY started_at ASC",
            (task_id,),
        )
        return [_decode(r) for r in rows]

    async def list_active(self, task_id: str) -> list[dict]:
        rows = await self._db.fetch_all(
            "SELECT * FROM runtime_sessions WHERE task_id = ? AND is_alive = 1 ORDER BY started_at ASC",
            (task_id,),
        )
        return [_decode(r) for r in rows]

    async def list_alive(self) -> list[dict]:
        rows = await self._db.fetch_all(
            "SELECT * FROM runtime_sessions WHERE is_alive = 1 ORDER BY started_at ASC"
        )
        return [_decode(r) for r in rows]

    async def mark_dead(self, session_id: str) -> None:
        await self._db.execute(
            "UPDATE runtime_sessions SET is_alive = 0, ended_at = ?, "
            "last_heartbeat = ? WHERE id = ?",
            (utcnow().isoformat(), utcnow().isoformat(), session_id),
        )


def _decode(row: dict) -> dict:
    val = row.get("metadata")
    if val:
        try:
            row["metadata"] = json.loads(val)
            row["runtime"] = row["metadata"].get("runtime")
            row["cwd"] = row["metadata"].get("cwd")
        except (TypeError, ValueError):
            pass
    row["is_alive"] = bool(row.get("is_alive", 0))
    return row
