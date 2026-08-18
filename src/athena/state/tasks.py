from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from athena.protocol.errors import IllegalStateTransition
from athena.protocol.messages import utcnow
from athena.protocol.tasks import FINAL_STATUSES, TaskStatus
from athena.state.database import Database
from athena.state.sessions import (
    _JSON_FIELDS,
    _serialize_capability_policy,
    _serialize_context_refs,
    _serialize_criteria,
    _serialize_delivery,
    _serialize_model_policy,
    _serialize_resource_budget,
    _serialize_workspace,
)


class TaskStore:
    """Persistent task records with transition validation (BUILDSPEC section 15)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def insert_task(
        self,
        task_id: str,
        session_id: str | None,
        parent_task_id: str | None,
        objective: str,
        *,
        autonomy: str = "supervised",
        acceptance_criteria: Any = None,
        context_refs: Any = None,
        workspace: Any = None,
        capability_policy: Any = None,
        model_policy: Any = None,
        resource_budget: Any = None,
        deadline: Any = None,
        delivery: Any = None,
        metadata: dict | None = None,
        status: TaskStatus = TaskStatus.CREATED,
    ) -> None:
        now = utcnow().isoformat()
        await self._db.execute(
            "INSERT INTO tasks("
            "id, session_id, parent_task_id, status, autonomy, objective, "
            "acceptance_criteria, context_refs, workspace, capability_policy, "
            "model_policy, resource_budget, deadline, delivery, "
            "created_at, updated_at, metadata"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                session_id,
                parent_task_id,
                status.value,
                autonomy,
                objective,
                _serialize_criteria(acceptance_criteria) if acceptance_criteria else None,
                _serialize_context_refs(context_refs) if context_refs else None,
                _serialize_workspace(workspace),
                _serialize_capability_policy(capability_policy) if capability_policy else None,
                _serialize_model_policy(model_policy) if model_policy else None,
                _serialize_resource_budget(resource_budget) if resource_budget else None,
                deadline.isoformat() if deadline else None,
                _serialize_delivery(delivery),
                now,
                now,
                json.dumps(dict(metadata or {})),
            ),
        )

    async def transition(self, task_id: str, new_status: TaskStatus) -> None:
        """Atomically transition a task, enforcing protocol LEGAL_TRANSITIONS."""
        async with self._db.transaction():
            row = await self._db.fetch_one_raw(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            )
            if row is None:
                raise KeyError(f"Task not found: {task_id}")
            current = TaskStatus(row["status"])
            allowed = current.legal_transitions()
            if new_status not in allowed:
                raise ValueError(
                    f"Illegal transition {current.value} -> {new_status.value}; "
                    f"allowed: {sorted(s.value for s in allowed)}"
                )
            now = utcnow().isoformat()
            await self._db.execute_raw(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (new_status.value, now, task_id),
            )
            if new_status in FINAL_STATUSES:
                await self._db.execute_raw(
                    "UPDATE tasks SET completed_at = ? WHERE id = ?",
                    (now, task_id),
                )

    async def get(self, task_id: str) -> dict | None:
        row = await self._db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            return None
        return _decode_task_row(row)

    async def list_by_session(self, session_id: str) -> list[dict]:
        rows = await self._db.fetch_all(
            "SELECT * FROM tasks WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        )
        return [_decode_task_row(r) for r in rows]

    async def list_by_status(self, status: TaskStatus) -> list[dict]:
        rows = await self._db.fetch_all(
            "SELECT * FROM tasks WHERE status = ? ORDER BY created_at ASC",
            (status.value,),
        )
        return [_decode_task_row(r) for r in rows]

    async def list_children(self, parent_task_id: str) -> list[dict]:
        """Every task whose ``parent_task_id`` points at the given task."""
        rows = await self._db.fetch_all(
            "SELECT * FROM tasks WHERE parent_task_id = ? ORDER BY created_at ASC",
            (parent_task_id,),
        )
        return [_decode_task_row(r) for r in rows]

    async def count_children(self, parent_task_id: str) -> int:
        """Count the direct child tasks of ``parent_task_id``."""
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS n FROM tasks WHERE parent_task_id = ?",
            (parent_task_id,),
        )
        return int(row["n"]) if row and row.get("n") is not None else 0

    async def list_descendants(self, task_id: str) -> list[dict]:
        """Every descendant task reachable through the parent links, breadth-first."""
        out: list[dict] = []
        seen: set[str] = set()
        id_frontier = [task_id]
        while id_frontier:
            nxt: list[dict] = []
            for pid in id_frontier:
                for child in await self.list_children(pid):
                    if child["id"] not in seen:
                        seen.add(child["id"])
                        nxt.append(child)
            out.extend(nxt)
            id_frontier = [child["id"] for child in nxt]
        return out

    async def child_ids(self, task_id: str) -> list[str]:
        return [r["id"] for r in await self.list_children(task_id)]

    async def descendant_ids(self, task_id: str) -> list[str]:
        return [r["id"] for r in await self.list_descendants(task_id)]

    async def set_retry_count(self, task_id: str, count: int) -> None:
        """Persist ``worker_retries`` into the task's metadata column."""
        row = await self._db.fetch_one(
            "SELECT metadata FROM tasks WHERE id = ?", (task_id,)
        )
        if row is None:
            return
        try:
            meta = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        meta["worker_retries"] = count
        await self._db.execute(
            "UPDATE tasks SET metadata = ?, updated_at = ? WHERE id = ?",
            (json.dumps(meta), utcnow().isoformat(), task_id),
        )

    async def persist_result(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        summary: str,
        evidence: Any,
        artifacts: Any,
        mutations: Any,
        unresolved: Any,
        usage: Any,
    ) -> None:
        """Persist a result row for an existing task without a status transition.

        Used to attach a returned :class:`TaskResult` to an already-terminal task.
        """
        await self._db.execute(
            "UPDATE tasks SET result_status = ?, summary = ?, evidence = ?, "
            "artifacts = ?, mutations = ?, unresolved = ?, usage = ?, updated_at = ? "
            "WHERE id = ?",
            (
                status.value,
                summary,
                json.dumps(evidence),
                json.dumps(artifacts),
                json.dumps(mutations),
                json.dumps(list(unresolved)),
                json.dumps(usage),
                utcnow().isoformat(),
                task_id,
            ),
        )

    async def finalize_with_result(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        result_status: TaskStatus,
        summary: str,
        evidence: Any,
        artifacts: Any,
        mutations: Any,
        unresolved: Any,
        usage: Any,
    ) -> None:
        """Atomically transition a task to a terminal status and persist its
        result in a single transaction (BUILDSPEC §86): a crash cannot leave a
        terminal task with no result.
        """
        async with self._db.transaction():
            row = await self._db.fetch_one_raw(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            )
            if row is None:
                raise KeyError(f"Task not found: {task_id}")
            current = TaskStatus(row["status"])
            allowed = current.legal_transitions()
            if status not in allowed:
                raise ValueError(
                    f"Illegal transition {current.value} -> {status.value}; "
                    f"allowed: {sorted(s.value for s in allowed)}"
                )
            now = utcnow().isoformat()
            await self._db.execute_raw(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, now, task_id),
            )
            if status in FINAL_STATUSES:
                await self._db.execute_raw(
                    "UPDATE tasks SET completed_at = ? WHERE id = ?",
                    (now, task_id),
                )
            await self._db.execute_raw(
                "UPDATE tasks SET result_status = ?, summary = ?, evidence = ?, "
                "artifacts = ?, mutations = ?, unresolved = ?, usage = ? "
                "WHERE id = ?",
                (
                    result_status.value,
                    summary,
                    json.dumps(evidence),
                    json.dumps(artifacts),
                    json.dumps(mutations),
                    json.dumps(list(unresolved)),
                    json.dumps(usage),
                    task_id,
                ),
            )

    async def claim_next(self, target_statuses: tuple[TaskStatus, ...]) -> dict | None:
        """Atomically claim one schedulable task.

        SQLite has no SELECT ... FOR UPDATE; correctness comes from doing the
        read + compare-and-set status update inside a single transaction.
        """
        if not target_statuses:
            return None
        vals = [s.value for s in target_statuses]
        placeholders = ",".join("?" for _ in target_statuses)
        async with self._db.transaction():
            row = await self._db.fetch_one_raw(
                f"SELECT id, status FROM tasks "
                f"WHERE status IN ({placeholders}) "
                f"ORDER BY created_at ASC LIMIT 1",
                vals,
            )
            if row is None:
                return None
            task_id = row["id"]
            current = TaskStatus(row["status"])
            running = TaskStatus.RUNNING
            if running not in current.legal_transitions():
                return None
            now = utcnow().isoformat()
            await self._db.execute_raw(
                "UPDATE tasks SET status = ?, updated_at = ?, started_at = ? WHERE id = ?",
                (running.value, now, now, task_id),
            )
            claimed = await self._db.fetch_one_raw(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            )
            return _decode_task_row(claimed) if claimed else None

    async def claim_with_lease(
        self,
        target_statuses: tuple[TaskStatus, ...],
        *,
        worker_id: str,
        lease_duration_seconds: float = 300.0,
    ) -> dict | None:
        """Atomically claim a task with a worker-specific lease.

        Transitions QUEUED|INTERRUPTED -> RUNNING. If a task is already RUNNING
        but its lease has expired, it can be re-claimed by the new worker via
        lease transfer (CAS on claimed_by/lease_expires_at).
        """
        if not target_statuses:
            return None
        vals = [s.value for s in target_statuses]
        placeholders = ",".join("?" for _ in target_statuses)
        now = utcnow()
        now_iso = now.isoformat()
        async with self._db.transaction():
            # Look for a schedulable task: QUEUED/INTERRUPTED first, then
            # RUNNING with expired lease (reclaim abandoned work).
            row = await self._db.fetch_one_raw(
                f"SELECT id, status, claimed_by, lease_expires_at FROM tasks "
                f"WHERE status IN ({placeholders}) "
                f"ORDER BY created_at ASC LIMIT 1",
                vals,
            )
            if row is None:
                # No QUEUED/INTERRUPTED; try to reclaim an expired lease
                row = await self._db.fetch_one_raw(
                    f"SELECT id, status, claimed_by, lease_expires_at FROM tasks "
                    f"WHERE status = 'RUNNING' AND lease_expires_at IS NOT NULL "
                    f"AND lease_expires_at < ? "
                    f"ORDER BY created_at ASC LIMIT 1",
                    (now_iso,),
                )
            if row is None:
                return None
            task_id = row["id"]
            current = TaskStatus(row["status"])
            lease_expires = now + timedelta(seconds=lease_duration_seconds)
            # CAS update: only succeeds if state hasn't changed under us
            if current == TaskStatus.RUNNING:
                # Reclaiming expired lease — CAS on claimed_by/lease_expires_at
                cas_where = "id = ? AND status = 'RUNNING' AND lease_expires_at < ?"
                cas_params: tuple = (task_id, now_iso)
            else:
                cas_where = "id = ? AND status = ?"
                cas_params = (task_id, current.value)
            await self._db.execute_raw(
                f"UPDATE tasks SET status = 'RUNNING', updated_at = ?, "
                f"started_at = COALESCE(started_at, ?), "
                f"claimed_by = ?, claim_started_at = ?, lease_expires_at = ? "
                f"WHERE {cas_where}",
                (now_iso, now_iso, worker_id, now_iso, lease_expires.isoformat(), *cas_params),
            )
            claimed = await self._db.fetch_one_raw(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            )
            return _decode_task_row(claimed) if claimed else None

    async def acquire_with_ownership(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_duration_seconds: float = 300.0,
    ) -> dict | None:
        """Acquire a specific task with worker ownership.

        Only succeeds if the task is in QUEUED or INTERRUPTED status. If the task
        is already RUNNING under a different worker (with a non-expired lease),
        raises IllegalStateTransition. If RUNNING under this same worker, allows
        re-acquire (idempotent). If RUNNING with an expired lease, allows
        re-acquire by the new worker.
        """
        now = utcnow()
        now_iso = now.isoformat()
        async with self._db.transaction():
            row = await self._db.fetch_one_raw(
                "SELECT status, claimed_by, lease_expires_at FROM tasks WHERE id = ?",
                (task_id,),
            )
            if row is None:
                return None
            current = TaskStatus(row["status"])
            running = TaskStatus.RUNNING
            if current in (TaskStatus.QUEUED, TaskStatus.INTERRUPTED):
                if running not in current.legal_transitions():
                    return None
            elif current == running:
                lease_expires = row.get("lease_expires_at")
                expires = _parse_iso(lease_expires) if lease_expires else None
                owned_by_other = row.get("claimed_by") is not None and row.get("claimed_by") != worker_id
                if expires is not None and expires <= now:
                    pass  # lease expired: allow re-acquire by new worker
                elif owned_by_other:
                    raise IllegalStateTransition(
                        f"task {task_id} already claimed by worker {row.get('claimed_by')!r}"
                    )
            else:
                return None
            lease_expires = now + timedelta(seconds=lease_duration_seconds)
            await self._db.execute_raw(
                "UPDATE tasks SET status = ?, updated_at = ?, started_at = ?, "
                "claimed_by = ?, claim_started_at = ?, lease_expires_at = ? "
                "WHERE id = ?",
                (
                    running.value,
                    now_iso,
                    now_iso,
                    worker_id,
                    now_iso,
                    lease_expires.isoformat(),
                    task_id,
                ),
            )
            claimed = await self._db.fetch_one_raw(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            )
            return _decode_task_row(claimed) if claimed else None


def _parse_iso(value: str) -> datetime | None:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _decode_task_row(row: dict) -> dict:
    for key in _JSON_FIELDS:
        val = row.get(key)
        if val:
            try:
                row[key] = json.loads(val)
            except (TypeError, ValueError):
                pass
    return row


__all__ = ["TaskStore"]