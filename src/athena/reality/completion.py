"""Durable completion saga for proven reality transitions.

The journal is intentionally small and append-by-replacement.  It bridges the
one unsafe restart window that cannot be closed by the task row or branch row
alone: the real workspace may be proven committed while task finalization has
not yet persisted.  A COMMIT_PROVEN record is sufficient to finish the task
without asking the model to repeat the operation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class CompletionJournal:
    """Atomic, private journal of reality-bound task completion."""

    def __init__(self, state_root: str | os.PathLike[str]) -> None:
        self._path = Path(state_root) / "reality-completions.json"
        self._records: dict[str, dict[str, Any]] = {}
        self._load()

    def begin_verified(
        self,
        *,
        task_id: str,
        branch_id: str,
        decision: Any,
        certificate: dict[str, Any],
    ) -> None:
        self._records[task_id] = {
            "task_id": task_id,
            "branch_id": branch_id,
            "state": "VERIFIED",
            "reason": str(getattr(decision, "reason", "") or ""),
            "summary": str(getattr(decision, "summary", "") or ""),
            "unresolved": list(getattr(decision, "unresolved", ()) or ()),
            "certificate": dict(certificate),
        }
        self._persist()

    def mark_commit_proven(
        self,
        task_id: str,
        *,
        final_fingerprint: str | None,
    ) -> None:
        record = self._records.setdefault(task_id, {"task_id": task_id})
        record["state"] = "COMMIT_PROVEN"
        record["final_fingerprint"] = final_fingerprint
        self._persist()

    def mark_finalized(self, task_id: str) -> None:
        record = self._records.get(task_id)
        if record is None:
            return
        record["state"] = "FINALIZED"
        self._persist()

    def mark_recovery_required(
        self,
        task_id: str,
        *,
        error: str,
        unresolved: tuple[str, ...] | list[str] = (),
    ) -> None:
        """Persist an unresolved saga without losing its recovery evidence."""
        record = self._records.setdefault(task_id, {"task_id": task_id})
        record["state"] = "RECOVERY_REQUIRED"
        record["error"] = str(error)
        record["unresolved"] = [str(item) for item in unresolved]
        self._persist()

    def mark_aborted(self, task_id: str, *, reason: str = "") -> None:
        """Close a saga whose candidate was safely discarded before commit."""
        record = self._records.get(task_id)
        if record is None:
            return
        record["state"] = "ABORTED"
        if reason:
            record["reason"] = reason
        self._persist()

    def pending(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            dict(record)
            for record in self._records.values()
            if record.get("state")
            in {
                "VERIFIED",
                "COMMIT_PROVEN",
                "RECOVERY_REQUIRED",
            }
        )

    def record(self, task_id: str) -> dict[str, Any] | None:
        record = self._records.get(task_id)
        return dict(record) if record is not None else None

    def _load(self) -> None:
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return
        if not isinstance(value, dict):
            return
        self._records = {
            str(task_id): dict(record)
            for task_id, record in value.items()
            if isinstance(record, dict)
        }

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self._path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(self._records, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, self._path)
        try:
            directory_fd = os.open(self._path.parent, os.O_DIRECTORY)
        except (AttributeError, OSError):
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


__all__ = ["CompletionJournal"]
