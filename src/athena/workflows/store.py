"""Durable workflow definitions."""

from __future__ import annotations

import json

from athena.state.database import Database
from athena.workflows.models import Workflow


class WorkflowStore:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._ready = False

    async def _ensure(self) -> None:
        if self._ready:
            return
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS workflows ("
            "id TEXT PRIMARY KEY, name TEXT NOT NULL, scope TEXT NOT NULL, "
            "task_scope TEXT, project_scope TEXT, user_scope TEXT, "
            "definition TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        self._ready = True

    async def save(self, workflow: Workflow) -> None:
        await self._ensure()
        from athena.protocol.messages import utcnow
        now = utcnow().isoformat()
        await self._db.execute(
            "INSERT INTO workflows(id, name, scope, task_scope, project_scope, "
            "user_scope, definition, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, scope=excluded.scope, "
            "task_scope=excluded.task_scope, project_scope=excluded.project_scope, "
            "user_scope=excluded.user_scope, "
            "definition=excluded.definition, updated_at=excluded.updated_at",
            (workflow.id, workflow.name, workflow.scope.value, workflow.task_scope,
             workflow.project_scope, workflow.user_scope,
             json.dumps(workflow.to_record()), now, now),
        )

    async def get(
        self, workflow_id: str, *, task_id: str | None = None,
        project_id: str | None = None, user_id: str | None = None,
    ) -> Workflow | None:
        await self._ensure()
        row = await self._db.fetch_one(
            "SELECT definition, scope, task_scope, project_scope, user_scope "
            "FROM workflows WHERE id = ?", (workflow_id,))
        if row is None:
            return None
        workflow = Workflow.from_record(json.loads(row["definition"]))
        if not _visible(workflow, task_id=task_id, project_id=project_id,
                        user_id=user_id):
            return None
        return workflow

    async def list(self, *, task_id: str | None = None,
                  project_id: str | None = None,
                  user_id: str | None = None) -> list[Workflow]:
        await self._ensure()
        rows = await self._db.fetch_all(
            "SELECT definition FROM workflows ORDER BY name, id")
        out: list[Workflow] = []
        for row in rows:
            workflow = Workflow.from_record(json.loads(row["definition"]))
            if not _visible(workflow, task_id=task_id, project_id=project_id,
                            user_id=user_id):
                continue
            out.append(workflow)
        return out

    async def delete(self, workflow_id: str) -> None:
        await self._ensure()
        await self._db.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))

    async def delete_for_task(self, task_id: str) -> None:
        await self._ensure()
        await self._db.execute(
            "DELETE FROM workflows WHERE scope = 'task' AND task_scope = ?",
            (task_id,),
        )


def _visible(
    workflow: Workflow, *, task_id: str | None, project_id: str | None,
    user_id: str | None,
) -> bool:
    if workflow.scope.value == "task":
        return bool(task_id and workflow.task_scope == task_id)
    if workflow.scope.value == "project":
        return bool(project_id and workflow.project_scope == project_id)
    if workflow.scope.value == "user":
        return bool(user_id and workflow.user_scope == user_id)
    return workflow.scope.value in {"system", "candidate"}


__all__ = ["WorkflowStore"]
