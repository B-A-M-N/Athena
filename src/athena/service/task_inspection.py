"""Read-only forensic inspection projection for one Athena task."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

__all__ = ["TaskInspectionService"]


class TaskInspectionService:
    """Group canonical task events without inventing a second event history."""

    def __init__(
        self,
        *,
        get_task: Callable[[str], Awaitable[Any]],
        get_result: Callable[[str], Awaitable[Any]],
        stream_events: Callable[..., AsyncIterator[Any]],
    ) -> None:
        self._get_task = get_task
        self._get_result = get_result
        self._stream_events = stream_events

    async def inspect(self, task_id: str) -> dict[str, Any]:
        task = await self._get_task(task_id)
        result = await self._get_result(task_id)
        gathered = [event async for event in self._stream_events(task_id, 0)]
        details = [
            {
                "sequence": getattr(event, "sequence", None),
                "type": event.type,
                "timestamp": getattr(event, "timestamp", None),
                "payload": dict(event.payload or {}),
                "causal_id": getattr(event, "causal_id", None),
            }
            for event in gathered
        ]
        categories = {
            "models": [
                e
                for e in details
                if str(e["type"]).startswith("Model") or str(e["type"]).startswith("Inference")
            ],
            "capabilities": [
                e
                for e in details
                if "Capability" in str(e["type"]) or str(e["type"]).startswith("Tool")
            ],
            "policy": [
                e for e in details if "Policy" in str(e["type"]) or "Approval" in str(e["type"])
            ],
            "execution": [
                e
                for e in details
                if "Execution" in str(e["type"]) or str(e["type"]).startswith("Std")
            ],
            "mutations": [e for e in details if "Mutation" in str(e["type"])],
            "artifacts": [e for e in details if "Artifact" in str(e["type"])],
            "children": [
                e for e in details if "Child" in str(e["type"]) or "Delegat" in str(e["type"])
            ],
        }
        return {
            "task_id": task_id,
            "status": (task.metadata or {}).get("status"),
            "objective": task.objective,
            "result": result,
            "events": [e.type for e in gathered],
            "event_details": details,
            "forensics": categories,
        }
