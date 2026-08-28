from __future__ import annotations

import json

from athena.protocol.execution import ExecutionExitStatus
from athena.protocol.messages import utcnow
from athena.state.database import Database

__all__ = ["ExecutionStore"]


class ExecutionStore:
    """Persistent record of executions (P0-22).

    Backs the ``executions`` table so crash recovery (RecoveryManager) can tell
    the truth about in-flight executions. ``status`` stores the execution
    lifecycle: ``RUNNING`` while live, then the exit status on completion
    (``exited`` / ``failed`` / ``interrupted`` / ``timed_out``); ``exit_code``
    captures the numeric code where meaningful.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def start(
        self,
        execution_id: str,
        *,
        task_id: str,
        runtime_session_id: str | None,
        command: str,
        args: str | None = None,
        cwd: str | None = None,
        env: dict | None = None,
    ) -> None:
        now = utcnow().isoformat()
        await self._db.execute(
            "INSERT INTO executions("
            "id, task_id, runtime_session_id, command, args, cwd, env, status, "
            "started_at, ended_at, exit_code, stdout_path, stderr_path, metadata"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?, NULL, NULL, NULL, NULL, '{}')",
            (
                execution_id,
                task_id,
                runtime_session_id,
                command,
                args,
                cwd,
                json.dumps(env) if env else None,
                now,
            ),
        )

    async def finish(
        self,
        execution_id: str,
        *,
        status: ExecutionExitStatus,
        exit_code: int | None = None,
        metadata: dict | None = None,
    ) -> None:
        now = utcnow().isoformat()
        await self._db.execute(
            "UPDATE executions SET status = ?, ended_at = ?, exit_code = ?, metadata = ? WHERE id = ?",
            (
                status.value,
                now,
                exit_code,
                json.dumps(dict(metadata or {})),
                execution_id,
            ),
        )

    async def update_runtime_session(self, execution_id: str, runtime_session_id: str) -> None:
        await self._db.execute(
            "UPDATE executions SET runtime_session_id = ? WHERE id = ?",
            (runtime_session_id, execution_id),
        )

    async def get(self, execution_id: str) -> dict | None:
        row = await self._db.fetch_one("SELECT * FROM executions WHERE id = ?", (execution_id,))
        if row is None:
            return None
        return _decode(row)

    async def list_for_task(self, task_id: str) -> list[dict]:
        rows = await self._db.fetch_all(
            "SELECT * FROM executions WHERE task_id = ? ORDER BY started_at ASC",
            (task_id,),
        )
        return [_decode(r) for r in rows]

    async def list_running_for_task(self, task_id: str) -> list[dict]:
        rows = await self._db.fetch_all(
            "SELECT * FROM executions WHERE task_id = ? AND status = 'RUNNING' ORDER BY started_at ASC",
            (task_id,),
        )
        return [_decode(r) for r in rows]

    async def list_by_status(self, status: str) -> list[dict]:
        rows = await self._db.fetch_all(
            "SELECT * FROM executions WHERE status = ? ORDER BY started_at ASC",
            (status,),
        )
        return [_decode(r) for r in rows]

    async def mark_interrupted(self, execution_id: str) -> None:
        await self._db.execute(
            "UPDATE executions SET status = ?, ended_at = ? WHERE id = ?",
            (ExecutionExitStatus.INTERRUPTED.value, utcnow().isoformat(), execution_id),
        )


def _decode(row: dict) -> dict:
    for key in ("args", "env", "metadata"):
        val = row.get(key)
        if val:
            try:
                row[key] = json.loads(val)
            except (TypeError, ValueError):
                pass
    return row
