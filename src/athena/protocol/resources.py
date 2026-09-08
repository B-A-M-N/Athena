"""Shared contracts for task-owned resource teardown."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TaskResourceCloseResult:
    """Proof returned by one task-owned resource manager after cleanup.

    A resource is confirmed only when its owner can prove that every resource
    it attempted to close is gone (or its durable close record is committed).
    ``unproven`` is deliberately structured so the service can retain a live
    ownership obligation instead of converting teardown uncertainty to success.
    """

    task_id: str
    resource_type: str
    resource_ids: tuple[str, ...] = ()
    closed_ids: tuple[str, ...] = ()
    unproven: tuple[dict[str, Any], ...] = ()
    errors: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def confirmed(self) -> bool:
        return (
            not self.unproven
            and not self.errors
            and set(self.resource_ids).issubset(self.closed_ids)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **dict(self.metadata),
            "task_id": self.task_id,
            "resource_type": self.resource_type,
            "resource_ids": list(self.resource_ids),
            "closed_ids": list(self.closed_ids),
            "unproven": [dict(item) for item in self.unproven],
            "errors": [dict(item) for item in self.errors],
            "confirmed": self.confirmed,
        }


__all__ = ["TaskResourceCloseResult"]
