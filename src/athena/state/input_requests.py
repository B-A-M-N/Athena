"""Durable operator-input requests (WAITING_INPUT continuation).

The model can determine mid-task that required information is missing. This
store persists the question and the task's continuation identity so the same
Task can be resumed with the operator's answer — instead of forcing the model
to guess or to finish the task with a question in place of a result.

Durability protocol (matches the approval continuation pattern):

    OPEN → ANSWERED_PENDING_RESUME → task reacquired/requeued →
    answer consumed → CONSUMED

The kernel reads the durable answer from this store rather than from an
in-memory dictionary, so a process crash between DB-write and kernel-wakeup
cannot strand a task in WAITING_INPUT with no live waiter.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from athena.state.database import Database


class InputRequestStore:
    """Durable records of tasks paused awaiting operator input.

    Status lifecycle:
        OPEN                  the question is live; no answer yet
        ANSWERED_PENDING_RESUME  answer durably stored; task not yet consumed it
        CONSUMED              answer read by the kernel; terminal
    """

    STATUS_OPEN = "OPEN"
    STATUS_ANSWERED_PENDING_RESUME = "ANSWERED_PENDING_RESUME"
    STATUS_CONSUMED = "CONSUMED"

    def __init__(self, db: Database) -> None:
        self._db = db
        self._ensured = False

    async def ensure_table(self) -> None:
        if self._ensured:
            return
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS input_requests("
            "id TEXT PRIMARY KEY, "
            "task_id TEXT NOT NULL, "
            "session_id TEXT, "
            "question TEXT NOT NULL, "
            "choices TEXT, "
            "context TEXT, "
            "expected TEXT, "
            "status TEXT NOT NULL, "
            "answer TEXT, "
            "answer_ref TEXT, "
            "created_at TEXT NOT NULL, "
            "resolved_at TEXT, "
            "consumed_at TEXT)"
        )
        # Migrate older schemas without the new columns.
        for column, definition in (
            ("answer_ref", "TEXT"),
            ("consumed_at", "TEXT"),
        ):
            try:
                await self._db.execute(
                    f"ALTER TABLE input_requests ADD COLUMN {column} {definition}"
                )
            except Exception:
                pass
        self._ensured = True

    async def record(
        self,
        *,
        task_id: str,
        question: str,
        session_id: str | None = None,
        choices: tuple[str, ...] | list[str] | None = None,
        context: Mapping[str, Any] | None = None,
        expected: str | None = None,
        request_id: str | None = None,
    ) -> str:
        """Persist one open input request; returns its id."""
        await self.ensure_table()
        rid = request_id or new_id("input")
        await self._db.execute(
            "INSERT INTO input_requests("
            "id, task_id, session_id, question, choices, context, expected, "
            "status, answer, answer_ref, created_at, resolved_at, consumed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', NULL, NULL, ?, NULL, NULL)",
            (
                rid,
                task_id,
                session_id,
                str(question),
                json.dumps([str(c) for c in (choices or ())]),
                json.dumps(dict(context or {})),
                expected,
                utcnow().isoformat(),
            ),
        )
        return rid

    async def pending_for_task(self, task_id: str) -> dict | None:
        """The open input request for a task, if any."""
        await self.ensure_table()
        row = await self._db.fetch_one(
            "SELECT * FROM input_requests WHERE task_id = ? AND status = 'OPEN' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (task_id,),
        )
        return _decode(row) if row else None

    async def resolve(
        self, request_id: str, answer: str, *, answer_ref: str | None = None
    ) -> dict | None:
        """Record the operator's answer durably.

        Sets status to ANSWERED_PENDING_RESUME (not directly to CONSUMED) so a
        restart can detect the half-resumed state and requeue the task.  The
        kernel later calls ``consume`` once the answer has been durably injected
        into the task's session.
        """
        await self.ensure_table()
        now = utcnow().isoformat()
        cursor = await self._db.execute(
            "UPDATE input_requests "
            "SET status = 'ANSWERED_PENDING_RESUME', answer = ?, answer_ref = ?, "
            "resolved_at = ? "
            "WHERE id = ? AND status = 'OPEN'",
            (str(answer), answer_ref, now, request_id),
        )
        if cursor.rowcount != 1:
            return None
        updated = await self._db.fetch_one(
            "SELECT * FROM input_requests WHERE id = ?", (request_id,)
        )
        return _decode(updated) if updated else None

    async def consume(self, request_id: str) -> bool:
        """Mark an answered input request as consumed exactly once."""
        await self.ensure_table()
        cursor = await self._db.execute(
            "UPDATE input_requests SET status = 'CONSUMED', consumed_at = ? "
            "WHERE id = ? AND status = 'ANSWERED_PENDING_RESUME'",
            (utcnow().isoformat(), request_id),
        )
        return cursor.rowcount == 1

    async def pending_resumable(self, task_id: str) -> dict | None:
        """An answered-but-not-consumed input request for a task, if any.

        Used by startup recovery to requeue a task that was parked in
        WAITING_INPUT and received an answer while the process was down.
        """
        await self.ensure_table()
        row = await self._db.fetch_one(
            "SELECT * FROM input_requests "
            "WHERE task_id = ? AND status = 'ANSWERED_PENDING_RESUME' "
            "ORDER BY resolved_at DESC, rowid DESC LIMIT 1",
            (task_id,),
        )
        return _decode(row) if row else None

    async def list_open(self, *, session_id: str | None = None) -> list[dict]:
        """Open requests, newest first, optionally scoped to one session."""
        await self.ensure_table()
        if session_id is not None:
            rows = await self._db.fetch_all(
                "SELECT * FROM input_requests WHERE status = 'OPEN' AND session_id = ? "
                "ORDER BY created_at DESC, rowid DESC",
                (session_id,),
            )
        else:
            rows = await self._db.fetch_all(
                "SELECT * FROM input_requests WHERE status = 'OPEN' "
                "ORDER BY created_at DESC, rowid DESC"
            )
        return [_decode(row) for row in rows]


def _decode(row: dict | Any) -> dict:
    record = dict(row)
    for field in ("choices", "context"):
        raw = record.get(field)
        if isinstance(raw, str):
            try:
                record[field] = json.loads(raw)
            except (TypeError, ValueError):
                record[field] = [] if field == "choices" else {}
    return record


__all__ = ["InputRequestStore"]
