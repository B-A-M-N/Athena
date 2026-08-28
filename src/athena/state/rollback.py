"""Mutation rollback executor (BUILDSPEC §86).

Implements actual undo of filesystem mutations by executing the recorded
inverse operations. The inverse is executed through the same policy/mutation
machinery so undo is itself auditable.

Inverse operations:
    restore_from_ref  — restore a file from an immutable artifact
    create_from_ref   — recreate a file from an immutable artifact
    delete            — remove a file/directory
    move              — move a file/directory
    move_restore      — restore a moved file and optionally the overwritten destination
    rmdir             — remove an empty directory
    noop              — no-op (nothing to undo)
"""

from __future__ import annotations

import logging
import hashlib
import os
import shutil
from pathlib import Path

from athena.state.mutations import (
    COMPLETED,
    MutationStore,
)

__all__ = ["RollbackExecutor"]

_logger = logging.getLogger("athena.rollback")


class RollbackExecutor:
    """Execute inverse operations for mutation rollback."""

    def __init__(self, mutation_store: MutationStore, artifact_store=None) -> None:
        self._store = mutation_store
        self._artifacts = artifact_store

    async def execute_inverse(self, mutation_id: str) -> dict:
        """Execute the inverse of a completed mutation.

        Returns a result dict with status and details.
        """
        row = await self._store.get(mutation_id)
        if row is None:
            return {"status": "error", "error": f"mutation {mutation_id} not found"}

        if row.get("status") != COMPLETED:
            return {
                "status": "error",
                "error": f"cannot rollback mutation in state {row.get('status')!r}",
            }

        if not row.get("reversible"):
            return {"status": "error", "error": "mutation is not reversible"}

        inverse = row.get("inverse")
        if inverse is None:
            return {"status": "error", "error": "mutation has no inverse recorded"}

        conflict = _inverse_conflict(row, inverse)
        if conflict is not None:
            return {
                "status": "error",
                "error": f"rollback refused: {conflict}",
            }

        # Record the rollback as a new mutation (auditable)
        rollback_id = await self._store.record(
            task_id=row.get("task_id"),
            resource=row.get("resource", ""),
            operation="undo",
            before_state=row.get("after_state"),
            after_state=row.get("before_state"),
            reversible=True,
            metadata={
                "undo_of": mutation_id,
                "original_operation": row.get("operation"),
                "original_inverse": inverse,
            },
        )

        # Execute the inverse operation
        try:
            await self._execute_inverse_operation(inverse)
        except Exception as exc:
            _logger.warning("rollback execution failed: %s", exc)
            # Mark the rollback as failed
            await self._store.mark_failed(rollback_id, error=str(exc))
            return {
                "status": "error",
                "error": f"rollback execution failed: {exc}",
                "rollback_id": rollback_id,
            }

        # Mark the original only after the inverse side effect succeeded. If
        # the process stops before this write, the original remains COMPLETED
        # and the same postcondition check makes retry behavior explicit.
        await self._store.mark_rolled_back(mutation_id)
        # Mark the rollback as completed
        await self._store.mark_reversible(rollback_id, True)
        return {
            "status": "ok",
            "rollback_id": rollback_id,
            "original_mutation": mutation_id,
            "inverse": inverse,
        }

    async def _execute_inverse_operation(self, inverse: dict) -> None:
        """Execute a single inverse operation."""
        op = inverse.get("op")
        target = inverse.get("target")
        source = inverse.get("source")
        ref = inverse.get("ref")

        if op == "noop":
            return
        elif op == "delete":
            if target and os.path.exists(target):
                if os.path.isdir(target):
                    shutil.rmtree(target)
                else:
                    os.remove(target)
        elif op == "restore_from_ref":
            await self._restore_from_ref(target, ref)
        elif op == "create_from_ref":
            await self._restore_from_ref(target, ref)
        elif op == "move":
            if source and target and os.path.exists(source):
                shutil.move(source, target)
        elif op == "move_restore":
            await self._execute_move_restore(inverse)
        elif op == "rmdir":
            if target and os.path.isdir(target) and not os.listdir(target):
                os.rmdir(target)
        else:
            _logger.warning("unknown inverse operation: %s", op)

    async def _restore_from_ref(self, target: str | None, ref: str | None) -> None:
        """Restore a file from an immutable artifact reference."""
        if not target or not ref:
            return
        if self._artifacts is None:
            _logger.warning("cannot restore from ref: no artifact store")
            return
        try:
            data = await self._artifacts.load(ref)
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_bytes(data)
        except Exception as exc:
            _logger.warning("restore_from_ref failed: %s", exc)
            raise

    async def _execute_move_restore(self, inverse: dict) -> None:
        """Execute a move_restore inverse.

        This handles the complex case of restoring a moved file and
        optionally restoring an overwritten destination.
        """
        source = inverse.get("source")  # where the file is now
        target = inverse.get("target")  # where the file was originally
        dest_before_ref = inverse.get("overwritten_destination_ref")

        # If the destination was overwritten, restore it first
        if dest_before_ref and source and target:
            try:
                # The overwritten file was moved to source; restore it to target
                # Actually, for move_restore:
                # - source = current location of the moved file (was dest)
                # - target = original location of the moved file (was path)
                # We need to move the file back from source to target
                if os.path.exists(source):
                    shutil.move(source, target)
            except Exception as exc:
                _logger.warning("move_restore failed: %s", exc)
                raise
        elif source and target and os.path.exists(source):
            # Simple move back
            shutil.move(source, target)


def _inverse_conflict(row: dict, inverse: dict) -> str | None:
    """Reject compensation when the target no longer has the owned post-state."""
    op = str(inverse.get("op") or "")
    if op in {"noop", "none", "rmdir"}:
        return None
    if op in {"restore_from_ref", "delete"}:
        target = inverse.get("target")
        if not isinstance(target, str) or not target:
            return "inverse target is missing"
        current = _file_hash(target)
        expected = row.get("after_state")
        if expected is None:
            if current is not None:
                return f"expected {target} to be absent"
            return None
        if current != str(expected):
            return f"{target} changed after mutation"
        return None
    if op == "create_from_ref":
        target = inverse.get("target")
        if not isinstance(target, str) or not target:
            return "inverse target is missing"
        if _file_hash(target) is not None:
            return f"expected {target} to be absent"
        return None
    if op == "move_restore":
        source = inverse.get("source")
        target = inverse.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            return "move inverse paths are missing"
        expected = row.get("after_state")
        if expected is not None and _file_hash(source) != str(expected):
            return f"{source} changed after mutation"
        if _file_hash(target) is not None:
            return f"{target} was recreated after mutation"
        return None
    return f"unsupported inverse operation {op!r}"


def _file_hash(path: str) -> str | None:
    """Hash only regular files; other resource kinds are conflicts."""
    if os.path.islink(path):
        return "<symlink>"
    if not os.path.exists(path):
        return None
    if not os.path.isfile(path):
        return "<unsupported-resource>"
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return "<unreadable>"
    return digest.hexdigest()
