from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from athena.protocol.errors import IllegalStateTransition, TaskOwnershipLost
from athena.protocol.messages import utcnow
from athena.protocol.task_codec import (
    encode_budget,
    encode_capability_policy,
    encode_context_refs,
    encode_criteria,
    encode_delivery,
    encode_model_policy,
    encode_workspace,
)
from athena.protocol.tasks import FINAL_STATUSES, TaskStatus
from athena.state.database import Database
from athena.state.sessions import (
    _JSON_FIELDS,
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
                encode_criteria(acceptance_criteria) if acceptance_criteria else None,
                encode_context_refs(context_refs) if context_refs else None,
                encode_workspace(workspace),
                encode_capability_policy(capability_policy) if capability_policy else None,
                encode_model_policy(model_policy) if model_policy else None,
                encode_budget(resource_budget) if resource_budget else None,
                deadline.isoformat() if deadline else None,
                encode_delivery(delivery),
                now,
                now,
                json.dumps(dict(metadata or {})),
            ),
        )

    async def transition(self, task_id: str, new_status: TaskStatus) -> None:
        """Atomically transition a task, enforcing protocol LEGAL_TRANSITIONS."""
        async with self._db.transaction():
            row = await self._db.fetch_one_raw("SELECT status FROM tasks WHERE id = ?", (task_id,))
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
            if current is TaskStatus.RUNNING and new_status is not TaskStatus.RUNNING:
                # Leaving RUNNING ends the live lease. A paused (WAITING_*),
                # interrupted, or terminal task must never look owned by a
                # worker; ownership history stays in events, not in a stale
                # live-looking lease that would block or mislead re-claim.
                await self._db.execute_raw(
                    "UPDATE tasks SET status = ?, updated_at = ?, "
                    "claimed_by = NULL, lease_expires_at = NULL WHERE id = ?",
                    (new_status.value, now, task_id),
                )
            else:
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

    async def update_metadata(self, task_id: str, updates: dict[str, Any]) -> bool:
        """Merge durable metadata fields without changing task authority state."""
        return await self._mutate_metadata(task_id, lambda meta: meta.update(dict(updates)))

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
        """Every descendant task reachable through the parent links.

        SQLite owns traversal in one recursive statement.  This avoids the
        previous one-query-per-parent N+1 pattern on cancellation, budget,
        and orchestration paths that walk task trees.
        """
        rows = await self._db.fetch_all(
            """
            WITH RECURSIVE descendants AS (
                SELECT * FROM tasks WHERE parent_task_id = ?
                UNION
                SELECT t.*
                FROM tasks t
                JOIN descendants d ON t.parent_task_id = d.id
            )
            SELECT * FROM descendants ORDER BY created_at ASC
            """,
            (task_id,),
        )
        return [_decode_task_row(r) for r in rows]

    async def child_ids(self, task_id: str) -> list[str]:
        return [r["id"] for r in await self.list_children(task_id)]

    async def descendant_ids(self, task_id: str) -> list[str]:
        """Return descendant identities without decoding full task records."""
        rows = await self._db.fetch_all(
            """
            WITH RECURSIVE descendants AS (
                SELECT id, parent_task_id FROM tasks WHERE parent_task_id = ?
                UNION
                SELECT t.id, t.parent_task_id
                FROM tasks t
                JOIN descendants d ON t.parent_task_id = d.id
            )
            SELECT id FROM descendants ORDER BY id ASC
            """,
            (task_id,),
        )
        return [str(row["id"]) for row in rows]

    async def _mutate_metadata(
        self,
        task_id: str,
        mutator: Callable[[dict[str, Any]], None],
    ) -> bool:
        """Atomically read-modify-write the task metadata document.

        The read, mutation, and write execute inside one transaction so two
        concurrent writers cannot overwrite each other's fields (the previous
        SELECT-decode-UPDATE sequence was not serialized as a unit). Returns
        False when the task does not exist.
        """
        async with self._db.transaction() as db:
            row = await db.fetch_one_raw("SELECT metadata FROM tasks WHERE id = ?", (task_id,))
            if row is None:
                return False
            try:
                metadata = json.loads(row.get("metadata") or "{}")
            except (TypeError, ValueError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            mutator(metadata)
            cursor = await db.execute_raw(
                "UPDATE tasks SET metadata = ?, updated_at = ? WHERE id = ?",
                (json.dumps(metadata, default=str), utcnow().isoformat(), task_id),
            )
            return cursor.rowcount == 1

    async def set_retry_count(self, task_id: str, count: int) -> None:
        """Persist ``worker_retries`` into the task's metadata column."""
        await self._mutate_metadata(task_id, lambda meta: meta.update(worker_retries=count))

    async def record_recovery_marker(self, task_id: str, marker: dict[str, Any]) -> None:
        """Persist uncertainty evidence without pretending the task succeeded."""

        def _apply(metadata: dict[str, Any]) -> None:
            markers = list(metadata.get("recovery_markers") or [])
            markers.append(dict(marker))
            metadata["recovery_markers"] = markers[-32:]
            metadata["recovery_required"] = True

        applied = await self._mutate_metadata(task_id, _apply)
        if not applied:
            raise KeyError(f"Task not found: {task_id}")

    async def persist_budget_usage(self, task_id: str, usage: dict[str, Any]) -> None:
        """Persist the budget ledger checkpoint without changing task state."""
        await self._mutate_metadata(task_id, lambda meta: meta.update(_budget_usage=dict(usage)))

    async def persist_runtime_recovery_hint(
        self,
        task_id: str,
        *,
        runtime_session_id: str,
        backend: str | None = None,
        runtime: str | None = None,
        cwd: str | None = None,
        released_resources: dict[str, Any] | None = None,
        checkpoint_id: str | None = None,
        resume_consequence: str | None = None,
    ) -> None:
        """Persist a fail-closed hint for the next context compilation.

        A resumed task must not assume that variables or a live process
        survived a service restart.  Keeping this beside the durable task
        record makes the warning available to every resumed model turn.
        """

        def _apply(metadata: dict[str, Any]) -> None:
            metadata["_runtime_recovery_hint"] = {
                "runtime_session_id": str(runtime_session_id),
                "backend": str(backend or "unknown"),
                "runtime": str(runtime or backend or "unknown"),
                "cwd": str(cwd) if cwd else None,
                "recovery_route": "execute",
                "replay_command": False,
                "recovery_action": "reestablish_runtime",
                "released_resources": dict(released_resources or {}),
                "checkpoint_id": str(checkpoint_id) if checkpoint_id else None,
                "resume_consequence": resume_consequence,
                "message": (
                    "Runtime state was lost across restart. Do not assume prior "
                    "process variables or session state exist; invoke execute with a fresh "
                    "task-owned runtime and reconstruct required state explicitly. Do not "
                    "replay a stale command automatically."
                ),
            }

        await self._mutate_metadata(task_id, _apply)

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
        allow_recovery_completion: bool = False,
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
        allow_recovery_completion: bool = False,
        recovery_finalization: bool = False,
        commit_pending: bool = False,
    ) -> None:
        """Atomically transition a task to a terminal status and persist its
        result in a single transaction (BUILDSPEC §86): a crash cannot leave a
        terminal task with no result.
        """
        async with self._db.transaction():
            row = await self._db.fetch_one_raw("SELECT status FROM tasks WHERE id = ?", (task_id,))
            if row is None:
                raise KeyError(f"Task not found: {task_id}")
            current = TaskStatus(row["status"])
            allowed = current.legal_transitions()
            if recovery_finalization and not (
                current is TaskStatus.RECOVERY_REQUIRED
                and status in FINAL_STATUSES
                and result_status is status
            ):
                raise ValueError(
                    "recovery finalization requires RECOVERY_REQUIRED -> exact final result"
                )
            if (
                not (
                    allow_recovery_completion
                    and current in {TaskStatus.INTERRUPTED, TaskStatus.RECOVERY_REQUIRED}
                    and status in FINAL_STATUSES
                )
                and not recovery_finalization
                and status not in allowed
            ):
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
            # The pending write-ahead record and the task/result commit share
            # this transaction. A crash after the task row is visible but
            # before observers run therefore leaves a durable replay point.
            if commit_pending:
                await self._db.execute_raw(
                    "UPDATE pending_task_finalizations SET phase = 'COMMITTED', "
                    "updated_at = ? WHERE task_id = ?",
                    (now, task_id),
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
            claimed = await self._db.fetch_one_raw("SELECT * FROM tasks WHERE id = ?", (task_id,))
            return _decode_task_row(claimed) if claimed else None

    async def claim_with_lease(
        self,
        target_statuses: tuple[TaskStatus, ...],
        *,
        worker_id: str,
        lease_duration_seconds: float = 300.0,
        reclaim_grace_seconds: float = 0.0,
    ) -> dict | None:
        """Atomically claim a task with a worker-specific lease.

        Transitions QUEUED|INTERRUPTED -> RUNNING. If a task is already RUNNING
        but its lease has expired by more than ``reclaim_grace_seconds``, it can
        be re-claimed by the new worker via lease transfer (CAS on
        claimed_by/lease_expires_at). The grace exists so a live owner whose
        heartbeat tick was merely delayed by a loaded event loop keeps
        ownership: callers should set the grace near their renewal interval so
        reclaim requires the owner to have missed substantially more than one
        tick — a genuinely dead process, not scheduler jitter.
        """
        if not target_statuses:
            return None
        vals = [s.value for s in target_statuses]
        placeholders = ",".join("?" for _ in target_statuses)
        now = utcnow()
        now_iso = now.isoformat()
        reclaim_deadline = (now - timedelta(seconds=max(reclaim_grace_seconds, 0.0))).isoformat()
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
                    "SELECT id, status, claimed_by, lease_expires_at FROM tasks "
                    "WHERE status = 'RUNNING' AND lease_expires_at IS NOT NULL "
                    "AND lease_expires_at < ? "
                    "ORDER BY created_at ASC LIMIT 1",
                    (reclaim_deadline,),
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
                cas_params: tuple = (task_id, reclaim_deadline)
            else:
                cas_where = "id = ? AND status = ?"
                cas_params = (task_id, current.value)
            cursor = await self._db.execute_raw(
                f"UPDATE tasks SET status = 'RUNNING', updated_at = ?, "
                f"started_at = COALESCE(started_at, ?), "
                f"claimed_by = ?, claim_started_at = ?, lease_expires_at = ? "
                f"WHERE {cas_where}",
                (now_iso, now_iso, worker_id, now_iso, lease_expires.isoformat(), *cas_params),
            )
            if cursor.rowcount != 1:
                # The row changed under us between SELECT and UPDATE; the CAS
                # must not silently return a task this worker does not own.
                return None
            claimed = await self._db.fetch_one_raw("SELECT * FROM tasks WHERE id = ?", (task_id,))
            return _decode_task_row(claimed) if claimed else None

    async def renew_lease(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_duration_seconds: float = 300.0,
    ) -> bool:
        """Extend a RUNNING task's lease, CAS on the owning worker.

        Also re-adopts an unowned RUNNING task whose lease window has passed
        (``claimed_by IS NULL`` with no fresh lease — the state a task is in
        right after a WAITING_* → RUNNING resume, where the lease was
        deliberately cleared on park). Adoption is itself a CAS: two
        concurrent renewers cannot both win, and adoption never races a
        legitimately-parked task whose resume slot another worker may hold.

        Returns ``True`` only when the update affected exactly one row this
        worker owns (or just adopted) while the task is still RUNNING. A
        ``False`` return means either the task is not RUNNING, or ownership
        was lost to another worker; the caller must stop driving the task
        unless it verifies the task is parked/paused.
        """
        now = utcnow()
        new_expiry = (now + timedelta(seconds=lease_duration_seconds)).isoformat()
        cursor = await self._db.execute(
            "UPDATE tasks SET lease_expires_at = ?, updated_at = ?, "
            "claimed_by = ?, claim_started_at = COALESCE(claim_started_at, ?) "
            "WHERE id = ? AND status = 'RUNNING' "
            "AND (claimed_by = ? OR (claimed_by IS NULL AND (lease_expires_at IS NULL "
            "OR lease_expires_at < ?)))",
            (
                new_expiry,
                now.isoformat(),
                worker_id,
                now.isoformat(),
                task_id,
                worker_id,
                new_expiry,
            ),
        )
        return cursor.rowcount == 1

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
                owned_by_other = (
                    row.get("claimed_by") is not None and row.get("claimed_by") != worker_id
                )
                if expires is not None and expires <= now:
                    pass  # lease expired: allow re-acquire by new worker
                elif owned_by_other:
                    raise IllegalStateTransition(
                        f"task {task_id} already claimed by worker {row.get('claimed_by')!r}"
                    )
            else:
                return None
            lease_expires = now + timedelta(seconds=lease_duration_seconds)
            cursor = await self._db.execute_raw(
                "UPDATE tasks SET status = ?, updated_at = ?, "
                "started_at = COALESCE(started_at, ?), "
                "claimed_by = ?, claim_started_at = ?, lease_expires_at = ? "
                "WHERE id = ? AND status = ?",
                (
                    running.value,
                    now_iso,
                    now_iso,
                    worker_id,
                    now_iso,
                    lease_expires.isoformat(),
                    task_id,
                    current.value,
                ),
            )
            if cursor.rowcount != 1:
                # CAS lost: the row changed between SELECT and UPDATE. That is
                # an ownership boundary, not an empty result — surface it so a
                # targeted runner cannot execute a task it does not own.
                raise TaskOwnershipLost(
                    f"task {task_id} changed state during acquisition "
                    f"(CAS lost for worker {worker_id!r})"
                )
            claimed = await self._db.fetch_one_raw("SELECT * FROM tasks WHERE id = ?", (task_id,))
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
