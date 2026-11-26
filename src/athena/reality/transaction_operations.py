"""Transactional recovery mechanics selected by :class:`RealityGate`.

The gate remains the only reality-routing and classification authority. This
helper owns checkpoint-bound progress, compensation, restart reconciliation,
and finalization against the gate's existing durable state.
"""

from __future__ import annotations

from typing import Any

from athena.protocol.messages import utcnow

__all__ = ["RealityTransactionOperations", "TransactionRecoveryRequired"]


class TransactionRecoveryRequired(RuntimeError):
    """The real workspace drifted beyond the transaction's owned revision."""


class RealityTransactionOperations:
    """Perform checkpoint lifecycle operations for one owning reality gate."""

    def __init__(self, gate: Any) -> None:
        self._gate = gate

    def mark_recovery_required(self, task_id: str | None, *, error: str) -> None:
        gate = self._gate
        state = gate._state
        if task_id is None or task_id not in state.checkpoint_by_task:
            return
        record = state.transaction_records.setdefault(task_id, {})
        record["state"] = "RECOVERY_REQUIRED"
        record["error"] = str(error)
        record["updated_at"] = utcnow().isoformat()
        state.persist()

    async def compensate(self, task_id: str | None) -> bool:
        """Restore the exact checkpoint-owned revision, or fail closed."""
        gate = self._gate
        state = gate._state
        if task_id is None:
            return False
        async with state.locks.lock(task_id):
            checkpoint_id = state.checkpoint_by_task.get(task_id)
            if checkpoint_id is None or gate._checkpoints is None:
                return False
            root = gate._base_root(task_id)
            record = state.transaction_records.setdefault(task_id, {})
            owned = record.get("last_owned_fingerprint")
            expected = await gate._checkpoints.fingerprint(root)
            if owned is None or expected != owned:
                record["state"] = "RECOVERY_REQUIRED"
                record["updated_at"] = utcnow().isoformat()
                state.persist()
                raise TransactionRecoveryRequired(
                    "transaction workspace differs from its last owned revision"
                )
            try:
                await gate._checkpoints.restore(
                    checkpoint_id,
                    root,
                    expected_fingerprint=expected,
                )
            except Exception:  # broad-exception: durable recovery marker precedes re-raise
                record["state"] = "RECOVERY_REQUIRED"
                record["updated_at"] = utcnow().isoformat()
                state.persist()
                raise
            await gate._checkpoints.release(checkpoint_id, owner=task_id)
            state.checkpoint_by_task.pop(task_id, None)
            state.checkpoint_root_by_task.pop(task_id, None)
            state.transaction_records.pop(task_id, None)
            state.persist()
            return True

    async def note_progress(
        self,
        task_id: str | None,
        workspace_root: str,
        *,
        mutation: bool,
        mutation_id: str | None = None,
        resource: str | None = None,
    ) -> None:
        """Record the complete post-state owned by a transactional mutation."""
        gate = self._gate
        state = gate._state
        if not mutation or task_id is None or gate._checkpoints is None:
            return
        if state.checkpoint_by_task.get(task_id) is None:
            return
        fingerprint = await gate._checkpoints.fingerprint(workspace_root)
        record = state.transaction_records.setdefault(task_id, {})
        record["last_owned_fingerprint"] = fingerprint
        record["state"] = "ACTIVE"
        record["postconditions"] = {}
        if mutation_id:
            mutation_ids = record.setdefault("mutation_ids", [])
            if mutation_id not in mutation_ids:
                mutation_ids.append(mutation_id)
        if resource:
            resources = record.setdefault("resources", [])
            if resource not in resources:
                resources.append(resource)
        record["updated_at"] = utcnow().isoformat()
        state.persist()

    def mark_proven(self, task_id: str | None) -> None:
        gate = self._gate
        state = gate._state
        if task_id is None or task_id not in state.checkpoint_by_task:
            return
        record = state.transaction_records.setdefault(task_id, {})
        record["state"] = "COMMIT_PROVEN"
        record["updated_at"] = utcnow().isoformat()
        state.persist()

    async def reconcile_startup(self) -> int:
        """Reconcile durable transaction ownership before new work routes."""
        gate = self._gate
        state = gate._state
        if gate._checkpoints is None:
            return 0
        recovered = 0
        for task_id, record in state.transaction_records.items():
            if record.get("state") not in {"ACTIVE", "COMMIT_PROVEN"}:
                continue
            root = str(record.get("workspace_root") or "")
            owned = record.get("last_owned_fingerprint")
            if not root or not owned:
                continue
            try:
                current = await gate._checkpoints.fingerprint(root)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                record["state"] = "RECOVERY_REQUIRED"
                record["error"] = f"transaction workspace unavailable: {exc}"
            else:
                if current == owned:
                    continue
                record["state"] = "RECOVERY_REQUIRED"
                record["error"] = (
                    "transaction workspace differs from its last owned revision after restart"
                )
            record["updated_at"] = utcnow().isoformat()
            recovered += 1
        if recovered:
            state.persist()
        return recovered

    async def finalize(self, task_id: str | None) -> None:
        """Release a successful in-place transaction's recovery binding."""
        gate = self._gate
        state = gate._state
        if task_id is None:
            return
        checkpoint_id = state.checkpoint_by_task.get(task_id)
        if checkpoint_id is not None and gate._checkpoints is not None:
            mark_terminal = getattr(gate._checkpoints, "mark_terminal", None)
            if mark_terminal is not None:
                mark_terminal(checkpoint_id, state="FINALIZED")
            await gate._checkpoints.release(checkpoint_id, owner=task_id)
        state.checkpoint_by_task.pop(task_id, None)
        state.checkpoint_root_by_task.pop(task_id, None)
        state.transaction_records.pop(task_id, None)
        state.persist()
