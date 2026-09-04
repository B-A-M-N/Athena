"""Durable workflow definitions."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, List, Mapping

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
        workspace_id: str | None = None,
        workspace_revision: str | None = None,
        verification: Mapping[str, Any] | None = None,
        observed_at: str | None = None,
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

            signature = str(provenance.get("trace_signature") or "")
            if signature:
                await self._insert_observation(
                    signature,
                    task_id=task_id,
                    steps=steps,
                    workspace_id=workspace_id,
                    workspace_revision=workspace_revision,
                    verification=verification,
                    observed_at=observed_at,
                )
            updated = merge_observation(
                workflow,
                task_id=task_id,
                steps=steps,
            )
            observations = await self._observations_for_signature(signature) if signature else []
            if observations:
                updated = replace(
                    updated,
                    provenance={
                        **dict(updated.provenance),
                        "pending_observation": False,
                        "observations": observations,
                    },
                    lifecycle_state="CANDIDATE",
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

    async def save_pending_observation(
        self,
        signature: str,
        *,
        task_id: str,
        steps: tuple[Any, ...],
        workspace_id: str | None = None,
        workspace_revision: str | None = None,
        verification: Mapping[str, Any] | None = None,
        observed_at: str | None = None,
    ) -> Workflow:
        """Persist the first trace without making it an active workflow.

        The first observation must survive a process restart, but it is not
        repeatability proof. It remains a candidate scoped to its source task
        until a distinct task observation is attached.
        """
        await self._ensure()
        from athena.protocol.messages import utcnow

        now = utcnow().isoformat()
        await self._insert_observation(
            signature,
            task_id=task_id,
            steps=steps,
            workspace_id=workspace_id,
            workspace_revision=workspace_revision,
            verification=verification,
            observed_at=observed_at or now,
        )
        # Keep inactive evidence bounded even when no later task reaches the
        # repeatability threshold.
        await self.expire_pending_observations(utcnow() - timedelta(days=30))
        await self.compact_pending_observations(512)

        existing = await self.find_candidate_by_signature(signature)
        if existing is not None:
            return existing
        from athena.affordances.models import AffordanceScope

        rows = await self._observations_for_signature(signature, raw=True)
        task_rows: List[Any] = []
        seen_tasks: set[str] = set()
        for row in rows:
            row_task = str(row["task_id"])
            if row_task in seen_tasks:
                continue
            seen_tasks.add(row_task)
            task_rows.append(row)
        first = task_rows[0] if task_rows else None
        if first is None:
            # The insert above is intentionally idempotent, but retain a
            # typed in-memory result for unusual database adapters.
            return _pending_workflow(
                task_id=task_id,
                signature=signature,
                steps=steps,
                workspace_id=workspace_id,
                workspace_revision=workspace_revision,
                verification=verification,
                observed_at=observed_at or now,
            )
        if len(task_rows) < 2:
            first_steps = _steps_from_row(first)
            return _pending_workflow(
                task_id=str(first["task_id"]),
                signature=signature,
                steps=first_steps,
                workspace_id=first["workspace_id"],
                workspace_revision=first["workspace_revision"],
                verification=json.loads(first["verification"] or "{}"),
                observed_at=str(first["observed_at"]),
            )

        from athena.workflows.mining import merge_observation

        first_steps = _steps_from_row(first)
        workflow = Workflow.create(
            name=f"task-{str(first['task_id'])[:12]} procedure",
            description="Candidate workflow induced from repeated successful capability calls",
            steps=first_steps,
            scope=AffordanceScope.CANDIDATE,
            task_scope=str(first["task_id"]),
            provenance={
                "origin": "successful_task_trace",
                "task_id": str(first["task_id"]),
                "call_count": len(first_steps),
                "trace_signature": signature,
                "observed_task_ids": [str(row["task_id"]) for row in task_rows],
                "successful_observations": len(task_rows),
                "observations": [_observation_record(row) for row in task_rows],
            },
        )
        for row in task_rows[1:]:
            workflow = merge_observation(
                workflow,
                task_id=str(row["task_id"]),
                steps=_steps_from_row(row),
            )
        workflow = replace(
            workflow,
            provenance={
                **dict(workflow.provenance),
                "pending_observation": False,
                "observations": [_observation_record(row) for row in task_rows],
                "successful_observations": len(task_rows),
            },
            lifecycle_state="CANDIDATE",
        )
        await self.save(workflow)
        return workflow

    async def expire_pending_observations(self, before: datetime) -> int:
        """Delete inactive workflow evidence older than the retention window."""
        await self._ensure()
        cursor = await self._db.execute(
            "DELETE FROM workflow_observations WHERE observed_at < ?",
            (before.isoformat(),),
        )
        return int(cursor.rowcount or 0)

    async def compact_pending_observations(self, limit: int = 512) -> int:
        """Keep a bounded newest set of inactive workflow observations."""
        await self._ensure()
        keep = max(1, int(limit))
        rows = await self._db.fetch_all(
            "SELECT id FROM workflow_observations "
            "ORDER BY observed_at DESC, created_at DESC, rowid DESC LIMIT -1 OFFSET ?",
            (keep,),
        )
        removed = 0
        for row in rows:
            cursor = await self._db.execute(
                "DELETE FROM workflow_observations WHERE id = ?", (str(row["id"]),)
            )
            removed += int(cursor.rowcount or 0)
        return removed

    async def _insert_observation(
        self,
        signature: str,
        *,
        task_id: str,
        steps: tuple[Any, ...],
        workspace_id: str | None,
        workspace_revision: str | None,
        verification: Mapping[str, Any] | None,
        observed_at: str | None,
    ) -> None:
        from athena.protocol.ids import new_id
        from athena.protocol.messages import utcnow

        now = utcnow().isoformat()
        records = [step.to_record() for step in steps]
        await self._db.execute(
            "INSERT INTO workflow_observations "
            "(id, trace_signature, task_id, workspace_id, workspace_revision, steps, "
            "argument_shape, verification, observed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(trace_signature, task_id) DO NOTHING",
            (
                new_id("workflow_observation"),
                signature,
                task_id,
                workspace_id,
                workspace_revision,
                json.dumps(records),
                json.dumps(_argument_shape(records)),
                json.dumps(dict(verification or {}), default=str),
                observed_at or now,
                now,
            ),
        )

    async def _observations_for_signature(
        self,
        signature: str,
        *,
        raw: bool = False,
    ) -> List[Any]:
        rows = await self._db.fetch_all(
            "SELECT rowid AS _observation_rowid, * FROM workflow_observations "
            "WHERE trace_signature = ? "
            "ORDER BY observed_at ASC, created_at ASC, _observation_rowid ASC",
            (signature,),
        )
        if raw:
            return list(rows)
        return [_observation_record(row) for row in rows]

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


def _pending_workflow(
    *,
    task_id: str,
    signature: str,
    steps: tuple[Any, ...],
    workspace_id: str | None,
    workspace_revision: str | None,
    verification: Mapping[str, Any] | None,
    observed_at: str,
) -> Workflow:
    from athena.affordances.models import AffordanceScope

    return Workflow.create(
        name=f"pending-{task_id[:12]} procedure",
        description="Pending workflow observation awaiting an independent repeat",
        steps=steps,
        scope=AffordanceScope.CANDIDATE,
        task_scope=task_id,
        lifecycle_state="PENDING_OBSERVATION",
        provenance={
            "origin": "successful_task_trace",
            "task_id": task_id,
            "call_count": len(steps),
            "trace_signature": signature,
            "observed_task_ids": [task_id],
            "successful_observations": 1,
            "pending_observation": True,
            "workspace_id": workspace_id,
            "workspace_revision": workspace_revision,
            "verification": dict(verification or {}),
            "observed_at": observed_at,
        },
    )


def _steps_from_row(row: Mapping[str, Any]) -> tuple[Any, ...]:
    from athena.workflows.models import WorkflowStep

    raw_steps = json.loads(row["steps"] or "[]")
    return tuple(
        WorkflowStep.from_record(step, index) for index, step in enumerate(raw_steps)
    )


def _observation_record(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "observation_id": str(row["id"]),
        "task_id": str(row["task_id"]),
        "workspace_id": row["workspace_id"],
        "workspace_revision": row["workspace_revision"],
        "verification": json.loads(row["verification"] or "{}"),
        "observed_at": str(row["observed_at"]),
        "steps": json.loads(row["steps"] or "[]"),
        "argument_shape": json.loads(row["argument_shape"] or "[]"),
    }


def _argument_shape(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(name): (
                str(item).casefold()
                if str(name).casefold() in {"operation", "action", "method", "type"}
                else _argument_shape(item)
            )
            for name, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_argument_shape(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return type(value).__name__
    return "string"


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
