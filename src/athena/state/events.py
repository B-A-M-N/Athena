from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

from aiosqlite import IntegrityError

from athena.protocol.events import Event, make_event
from athena.state.database import Database


class EventStore:
    """Append-only, idempotent event log (BUILDSPEC sections 78-83).

    Events are immutable. ``append_event`` is the SINGLE authoritative append
    path (P0-20): it assigns the per-task ``sequence`` atomically from the
    database (``SELECT COALESCE(MAX(sequence), 0) + 1``) inside a transaction, so the
    ``UNIQUE(task_id, sequence)`` constraint can never collide and no event is
    silently dropped — regardless of how many emitters write for the same task
    or across process restarts. No component may manufacture its own sequence.

    Consumers tolerate duplicate delivery, so re-driven events deduplicate by
    stable event id on append (section 81).
    """

    _COLS = (
        "id, task_id, session_id, type, sequence, timestamp, schema_version, "
        "payload, causal_id"
    )

    def __init__(self, db: Database) -> None:
        self._db = db

    async def append_event(
        self,
        type_: str,
        payload: Mapping[str, Any] | None = None,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        causal_id: str | None = None,
        id: str | None = None,
    ) -> Event:
        while True:
            try:
                async with self._db.transaction() as db:
                    row = await db.fetch_one_raw(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 AS seq "
                        "FROM events WHERE task_id = ?",
                        (task_id,),
                    )
                    seq = int(row["seq"]) if row and row.get("seq") else 1
                    event = make_event(
                        type=type_,
                        payload=payload,
                        task_id=task_id,
                        session_id=session_id,
                        sequence=seq,
                        id=id,
                        causal_id=causal_id,
                    )
                    await db.execute_raw(
                        f"INSERT INTO events({self._COLS}) "
                        f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        self._values(event),
                    )
                return event
            except IntegrityError as exc:
                # Retry only on a genuine (task_id, sequence) UNIQUE collision
                # from a concurrent writer. FK violations must propagate.
                if "UNIQUE" not in str(exc):
                    raise
                continue

    async def append(self, event: Event) -> None:
        if event.id and await self._db.fetch_one(
            "SELECT id FROM events WHERE id = ?", (event.id,)
        ):
            return
        await self.append_event(
            event.type,
            event.payload,
            task_id=event.task_id,
            session_id=event.session_id,
            causal_id=event.causal_id,
            id=event.id,
        )

    @staticmethod
    def _values(event: Event) -> tuple:
        return (
            event.id,
            event.task_id,
            event.session_id,
            event.type,
            event.sequence,
            event.timestamp.isoformat(),
            event.schema_version,
            json.dumps(dict(event.payload)),
            event.causal_id,
        )

    async def list_for_task(
        self,
        task_id: str,
        after_sequence: int = 0,
    ) -> list[Event]:
        rows = await self._db.fetch_all(
            "SELECT * FROM events WHERE task_id = ? AND sequence > ? "
            "ORDER BY sequence ASC",
            (task_id, after_sequence),
        )
        return [_row_to_event(r) for r in rows]

    async def list_for_session(self, session_id: str) -> list[Event]:
        rows = await self._db.fetch_all(
            "SELECT * FROM events WHERE session_id = ? "
            "ORDER BY timestamp ASC, sequence ASC",
            (session_id,),
        )
        return [_row_to_event(r) for r in rows]

    async def last_sequence(self, task_id: str) -> int:
        row = await self._db.fetch_one(
            "SELECT MAX(sequence) AS seq FROM events WHERE task_id = ?",
            (task_id,),
        )
        return int((row or {}).get("seq") or 0)


def _row_to_event(row: dict | Any) -> Event:
    payload = json.loads(row["payload"]) if row.get("payload") else {}
    return Event(
        id=row["id"],
        type=row["type"],
        sequence=row["sequence"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        task_id=row.get("task_id"),
        session_id=row.get("session_id"),
        schema_version=row.get("schema_version", 1),
        payload=payload,
        causal_id=row.get("causal_id"),
    )


__all__ = ["EventStore"]