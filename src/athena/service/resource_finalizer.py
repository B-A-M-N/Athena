"""Single owner for task-scoped computational resource teardown."""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

from athena.protocol.events import EV, make_event
from athena.protocol.tasks import FINAL_STATUSES, TaskStatus

_logger = logging.getLogger("athena.service.resources")


class TaskResourceFinalizer:
    """Close task-owned runtimes and capability sessions after finalization.

    TaskManager deliberately runs observers after the result is durable. This
    coordinator is the one observer responsible for the resource boundary, so
    a capability cannot quietly invent a second task cleanup path.
    """

    def __init__(self, *, event_sink=None, history_limit: int = 256) -> None:
        self._event_sink = event_sink
        self._outcomes: deque[dict[str, Any]] = deque(maxlen=history_limit)
        self._inflight: set[str] = set()

    async def _emit(self, event_type: str, task_id: str, payload: dict[str, Any]) -> None:
        if self._event_sink is None:
            return
        try:
            await self._event_sink(make_event(event_type, payload, task_id=task_id))
        except Exception as exc:  # event persistence cannot resurrect a resource
            _logger.warning("resource teardown event failed for %s: %s", task_id, exc)

    async def finalize(self, task, result) -> None:
        status = getattr(result, "status", None)
        if status not in FINAL_STATUSES and status is not TaskStatus.RECOVERY_REQUIRED:
            return
        task_id = str(task.id)
        if task_id in self._inflight:
            return
        self._inflight.add(task_id)
        outcome: dict[str, Any] = {
            "task_id": task_id,
            "status": getattr(status, "value", str(status)),
            "closed": [],
            "failures": [],
            "confirmed": False,
        }
        # Auxiliary capability sessions first; execution is the final sweep.
        service = getattr(self, "_service", None)
        resources = (
            ("debugger", getattr(service, "_debugger", None)),
            ("terminal", getattr(service, "_terminals", None)),
            ("browser", getattr(service, "_browser", None)),
            ("execution", getattr(service, "_execution", None)),
        )
        try:
            for name, resource in resources:
                if resource is None:
                    continue
                close_task = getattr(resource, "close_task", None)
                if close_task is None:
                    continue
                try:
                    close_result = await close_task(task_id)
                    if getattr(close_result, "confirmed", True) is False:
                        outcome["failures"].append(
                            {
                                "resource": name,
                                "error": "resource close returned unconfirmed",
                                "evidence": getattr(close_result, "__dict__", {}),
                            }
                        )
                    outcome["closed"].append(name)
                except Exception as exc:  # preserve every failure as evidence
                    outcome["failures"].append({"resource": name, "error": str(exc)})
                    _logger.warning("task %s %s cleanup failed: %s", task_id, name, exc)
            outcome["confirmed"] = not outcome["failures"]
            self._outcomes.append(outcome)
            event_type = (
                EV["TASK_RESOURCES_FINALIZED"]
                if outcome["confirmed"]
                else EV["TASK_RESOURCE_TEARDOWN_FAILED"]
            )
            await self._emit(event_type, task_id, outcome)
        finally:
            self._inflight.discard(task_id)

    def bind_service(self, service: Any) -> None:
        self._service = service

    def health(self) -> dict[str, Any]:
        last = self._outcomes[-1] if self._outcomes else None
        return {
            "last": dict(last) if last is not None else None,
            "inflight": sorted(self._inflight),
            "failures": sum(bool(item.get("failures")) for item in self._outcomes),
        }


__all__ = ["TaskResourceFinalizer"]
