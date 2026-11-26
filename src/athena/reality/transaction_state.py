"""Durable transaction-state storage for the reality gate.

Subordinate to :class:`athena.reality.gate.RealityGate`. This module owns
atomic load/persist mechanics for checkpoint bindings used by restart
reconciliation. It does not classify effects, authorize execution, or route
requests.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

_logger = logging.getLogger("athena.reality")

__all__ = ["TransactionStateStore"]


class TransactionStateStore:
    """Atomically persist checkpoint bindings used for restart recovery."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.checkpoint_by_task: dict[str, str] = {}
        self.checkpoint_root_by_task: dict[str, str] = {}
        self.records: dict[str, dict[str, Any]] = {}

    def load(self) -> None:
        """Restore transactional checkpoint bindings before new work routes."""
        if self.path is None:
            return
        try:
            records = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return
        if not isinstance(records, dict):
            return
        for task_id, record in records.items():
            if not isinstance(record, dict):
                continue
            checkpoint_id = record.get("checkpoint_id")
            workspace_root = record.get("workspace_root")
            if checkpoint_id and workspace_root:
                self.checkpoint_by_task[str(task_id)] = str(checkpoint_id)
                self.checkpoint_root_by_task[str(task_id)] = str(workspace_root)
                self.records[str(task_id)] = {
                    "transaction_id": str(record.get("transaction_id") or task_id),
                    "checkpoint_id": str(checkpoint_id),
                    "workspace_root": str(workspace_root),
                    "base_fingerprint": record.get("base_fingerprint"),
                    "last_owned_fingerprint": record.get("last_owned_fingerprint"),
                    "base_manifest": dict(record.get("base_manifest") or {}),
                    "postconditions": dict(record.get("postconditions") or {}),
                    "resources": [
                        str(resource)
                        for resource in record.get("resources") or ()
                        if isinstance(resource, str)
                    ],
                    "mutation_ids": [
                        str(mutation_id)
                        for mutation_id in record.get("mutation_ids") or ()
                        if isinstance(mutation_id, str)
                    ],
                    "started_at": record.get("started_at"),
                    "updated_at": record.get("updated_at"),
                    "state": str(record.get("state") or "ACTIVE"),
                }

    def persist(self) -> None:
        """Atomically write current checkpoint bindings and records."""
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        records = {
            task_id: {
                "transaction_id": self.records.get(task_id, {}).get("transaction_id", task_id),
                "checkpoint_id": checkpoint_id,
                "workspace_root": self.checkpoint_root_by_task.get(task_id),
                "base_fingerprint": self.records.get(task_id, {}).get("base_fingerprint"),
                "last_owned_fingerprint": self.records.get(task_id, {}).get(
                    "last_owned_fingerprint"
                ),
                "base_manifest": self.records.get(task_id, {}).get("base_manifest", {}),
                "postconditions": self.records.get(task_id, {}).get("postconditions", {}),
                "resources": self.records.get(task_id, {}).get("resources", []),
                "mutation_ids": self.records.get(task_id, {}).get("mutation_ids", []),
                "started_at": self.records.get(task_id, {}).get("started_at"),
                "updated_at": self.records.get(task_id, {}).get("updated_at"),
                "state": self.records.get(task_id, {}).get("state", "ACTIVE"),
            }
            for task_id, checkpoint_id in self.checkpoint_by_task.items()
        }
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(records, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
        try:
            directory_fd = os.open(self.path.parent, os.O_DIRECTORY)
        except (AttributeError, OSError):
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
