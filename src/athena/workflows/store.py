"""Durable workflow definitions."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Mapping

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
            (
                workflow.id,
                workflow.name,
                workflow.scope.value,
                workflow.task_scope,
                workflow.project_scope,
                workflow.user_scope,
                json.dumps(workflow.to_record()),
                now,
                now,
            ),
        )

    async def get(
        self,
        workflow_id: str,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> Workflow | None:
        await self._ensure()
        row = await self._db.fetch_one(
            "SELECT definition, scope, task_scope, project_scope, user_scope "
            "FROM workflows WHERE id = ?",
            (workflow_id,),
        )
        if row is None:
            return None
        workflow = Workflow.from_record(json.loads(row["definition"]))
        if not _visible(workflow, task_id=task_id, project_id=project_id, user_id=user_id):
            return None
        return workflow

    async def list(
        self,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> list[Workflow]:
        await self._ensure()
        rows = await self._db.fetch_all("SELECT definition FROM workflows ORDER BY name, id")
        out: list[Workflow] = []
        for row in rows:
            workflow = Workflow.from_record(json.loads(row["definition"]))
            if not _visible(workflow, task_id=task_id, project_id=project_id, user_id=user_id):
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

    async def find_candidate_by_signature(self, signature: str) -> Workflow | None:
        """Find a candidate procedure observed in another task."""
        await self._ensure()
        rows = await self._db.fetch_all(
            "SELECT definition FROM workflows WHERE scope = 'candidate'"
        )
        for row in rows:
            workflow = Workflow.from_record(json.loads(row["definition"]))
            if workflow.provenance.get("trace_signature") == signature:
                return workflow
        return None

    async def record_candidate_observation(
        self,
        workflow_id: str,
        *,
        task_id: str,
        steps: tuple[Any, ...] = (),
    ) -> Workflow | None:
        """Attach a distinct successful task observation to a candidate."""
        await self._ensure()
        row = await self._db.fetch_one(
            "SELECT definition FROM workflows WHERE id = ? AND scope = 'candidate'",
            (workflow_id,),
        )
        if row is None:
            return None
        workflow = Workflow.from_record(json.loads(row["definition"]))
        provenance = dict(workflow.provenance)
        task_ids = [str(item) for item in provenance.get("observed_task_ids") or ()]
        if task_id in task_ids:
            return workflow
        if steps:
            from athena.workflows.mining import merge_observation

            updated = merge_observation(
                workflow,
                task_id=task_id,
                steps=steps,
            )
            await self.save(updated)
            return updated
        if task_id not in task_ids:
            task_ids.append(task_id)
        provenance["observed_task_ids"] = task_ids[-64:]
        provenance["successful_observations"] = len(task_ids)
        updated = replace(workflow, provenance=provenance)
        await self.save(updated)
        return updated

    async def promote_candidate(
        self,
        workflow_id: str,
        *,
        task_id: str,
        scope: str,
        project_id: str | None = None,
        user_id: str | None = None,
        validation: Mapping[str, Any] | None = None,
    ) -> Workflow | None:
        """Promote a task-owned workflow candidate to a durable overlay."""
        workflow = await self.get(workflow_id, task_id=task_id)
        if workflow is None or workflow.scope.value != "candidate":
            return None
        provenance = dict(workflow.provenance)
        if validation is not None:
            provenance["promotion_replay_validation"] = dict(validation)
            provenance["promotion_replay_status"] = str(validation.get("status") or "unknown")
        if scope == "project":
            if not project_id:
                raise ValueError("project workflow promotion requires project_id")
            promoted = replace(
                workflow,
                scope=workflow.scope.__class__.PROJECT,
                task_scope=None,
                project_scope=project_id,
                user_scope=None,
                lifecycle_state="PROMOTED",
                provenance=provenance,
            )
        elif scope == "user":
            if not user_id:
                raise ValueError("user workflow promotion requires user_id")
            promoted = replace(
                workflow,
                scope=workflow.scope.__class__.USER,
                task_scope=None,
                project_scope=None,
                user_scope=user_id,
                lifecycle_state="PROMOTED",
                provenance=provenance,
            )
        else:
            raise ValueError("workflow promotion target must be project or user")
        await self.save(promoted)
        return promoted


def _visible(
    workflow: Workflow,
    *,
    task_id: str | None,
    project_id: str | None,
    user_id: str | None,
) -> bool:
    if workflow.scope.value == "task":
        return bool(task_id and workflow.task_scope == task_id)
    if workflow.scope.value == "project":
        return bool(project_id and workflow.project_scope == project_id)
    if workflow.scope.value == "user":
        return bool(user_id and workflow.user_scope == user_id)
    if workflow.scope.value == "candidate":
        return bool(task_id and workflow.task_scope == task_id)
    return workflow.scope.value == "system"


__all__ = ["WorkflowStore"]
