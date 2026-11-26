"""Durable workflow run status and output projection mechanics."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from athena.protocol.messages import utcnow
from athena.workflows.receipts import decode_workflow_value

__all__ = ["WorkflowRunStatus"]


class WorkflowRunStatus:
    """Persist run-level status facts beneath ``WorkflowRunStore``."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def mark_step(
        self,
        run_id: str,
        step_id: str,
        *,
        status: str,
        output: Any = None,
        failures: tuple[str, ...] = (),
    ) -> None:
        now = utcnow().isoformat()
        state = "COMPLETE" if status in {"completed", "failed", "skipped"} else status.upper()
        await self._db.execute(
            "INSERT INTO workflow_step_runs (run_id, step_id, status, output, failures, "
            "started_at, completed_at, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, step_id) DO UPDATE SET status=excluded.status, "
            "output=excluded.output, failures=excluded.failures, "
            "completed_at=excluded.completed_at, state=excluded.state",
            (
                run_id,
                step_id,
                status,
                json.dumps(output),
                json.dumps(list(failures)),
                now,
                now if status in {"completed", "failed", "skipped"} else None,
                state,
            ),
        )
        await self.touch_run(run_id, status="running")

    async def promote_item_output(self, run_id: str, step_id: str, output: Any) -> None:
        row = await self._db.fetch_one("SELECT outputs FROM workflow_runs WHERE id = ?", (run_id,))
        outputs = decode_workflow_value((row or {}).get("outputs"))
        if not isinstance(outputs, dict):
            outputs = {}
        outputs[step_id] = output
        await self._db.execute(
            "UPDATE workflow_runs SET outputs = ?, status = 'running', updated_at = ? WHERE id = ?",
            (json.dumps(outputs), utcnow().isoformat(), run_id),
        )

    async def touch_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        db: Any | None = None,
    ) -> None:
        database = db or self._db
        execute = getattr(database, "execute_raw", database.execute) if db else database.execute
        if status is None:
            await execute(
                "UPDATE workflow_runs SET updated_at = ? WHERE id = ?",
                (utcnow().isoformat(), run_id),
            )
        else:
            await execute(
                "UPDATE workflow_runs SET status = ?, updated_at = ? WHERE id = ?",
                (status, utcnow().isoformat(), run_id),
            )

    async def finish(
        self,
        run_id: str,
        *,
        status: str,
        outputs: Mapping[str, Any],
        workspace_revision: str | None = None,
    ) -> None:
        await self._db.execute(
            "UPDATE workflow_runs SET status = ?, outputs = ?, "
            "workspace_revision = COALESCE(?, workspace_revision), updated_at = ? "
            "WHERE id = ?",
            (
                status,
                json.dumps(dict(outputs)),
                workspace_revision,
                utcnow().isoformat(),
                run_id,
            ),
        )

    async def update_workspace_revision(
        self,
        run_id: str,
        revision: str,
        *,
        environment_identity: str | None = None,
    ) -> None:
        """Advance the run's CAS baseline after a durably completed step batch."""
        await self._db.execute(
            "UPDATE workflow_runs SET workspace_revision = ?, "
            "environment_identity = COALESCE(?, environment_identity), updated_at = ? "
            "WHERE id = ?",
            (revision, environment_identity, utcnow().isoformat(), run_id),
        )
