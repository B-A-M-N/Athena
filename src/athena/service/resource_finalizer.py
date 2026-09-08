"""Single owner for task-scoped computational resource teardown."""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

from athena.protocol.events import EV, make_event
from athena.protocol.resources import TaskResourceCloseResult
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
        self._unresolved: dict[tuple[str, str, str], dict[str, Any]] = {}

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
            "resources": {},
            "unresolved": [],
            "confirmed": False,
        }
        # Every process/session owner is closed through this one boundary.
        # Logical affordance cleanup remains a separate post-finalization
        # observer and must not be allowed to hide a failed process teardown.
        service = getattr(self, "_service", None)
        resources = (
            ("debugger", getattr(service, "_debugger", None)),
            ("terminal", getattr(service, "_terminals", None)),
            ("browser", getattr(service, "_browser", None)),
            ("external_delegate", getattr(service, "_external_delegate_manager", None)),
            ("generated_runtime", getattr(service, "_synthesis", None)),
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
                    evidence = _close_evidence(name, task_id, close_result)
                    outcome["resources"][name] = evidence
                    if evidence.get("confirmed"):
                        outcome["closed"].append(name)
                        self._clear_unresolved(task_id, name)
                    else:
                        outcome["failures"].append(
                            {"resource": name, "error": "resource close was not confirmed"}
                        )
                        self._record_unresolved(task_id, name, evidence)
                except Exception as exc:  # preserve every failure as evidence
                    evidence = {
                        "task_id": task_id,
                        "resource_type": name,
                        "resource_ids": [],
                        "closed_ids": [],
                        "unproven": [{"resource": name, "error": str(exc)}],
                        "errors": [{"resource": name, "error": str(exc)}],
                        "confirmed": False,
                    }
                    outcome["resources"][name] = evidence
                    outcome["failures"].append({"resource": name, "error": str(exc)})
                    self._record_unresolved(task_id, name, evidence)
                    _logger.warning("task %s %s cleanup failed: %s", task_id, name, exc)
            outcome["unresolved"] = self._unresolved_for_task(task_id)
            outcome["confirmed"] = not outcome["unresolved"]
            self._outcomes.append(outcome)
            event_type = (
                EV["TASK_RESOURCES_FINALIZED"]
                if outcome["confirmed"]
                else EV["TASK_RESOURCE_TEARDOWN_FAILED"]
            )
            await self._emit(event_type, task_id, outcome)
        finally:
            self._inflight.discard(task_id)

    async def retry(self, task, result) -> None:
        """Retry only unresolved resource obligations for a terminal task."""
        await self.finalize(task, result)

    def bind_service(self, service: Any) -> None:
        self._service = service

    def health(self) -> dict[str, Any]:
        last = self._outcomes[-1] if self._outcomes else None
        unresolved = list(self._unresolved.values())
        return {
            "last": dict(last) if last is not None else None,
            "inflight": sorted(self._inflight),
            "failures": sum(bool(item.get("failures")) for item in self._outcomes),
            "state": "recovery_required" if unresolved else "healthy",
            "unresolved_count": len(unresolved),
            "unresolved": unresolved,
        }

    def _record_unresolved(self, task_id: str, resource: str, evidence: dict[str, Any]) -> None:
        ids = _unresolved_ids(evidence)
        for resource_id in ids:
            key = (task_id, resource, str(resource_id))
            previous = self._unresolved.get(key)
            self._unresolved[key] = {
                "task_id": task_id,
                "resource_type": resource,
                "resource_id": str(resource_id),
                "first_failure": (previous or {}).get("first_failure") or _timestamp(),
                "last_retry": _timestamp(),
                "attempts": int((previous or {}).get("attempts", 0)) + 1,
                "proof_state": "unproven",
                "last_error": _last_error(evidence),
                "evidence": evidence,
            }

    def _clear_unresolved(self, task_id: str, resource: str) -> None:
        for key in list(self._unresolved):
            if key[:2] == (task_id, resource):
                self._unresolved.pop(key, None)

    def _unresolved_for_task(self, task_id: str) -> list[dict[str, Any]]:
        return [dict(value) for value in self._unresolved.values() if value["task_id"] == task_id]


def _close_evidence(name: str, task_id: str, result: Any) -> dict[str, Any]:
    if isinstance(result, TaskResourceCloseResult):
        return result.to_dict()
    if result is None:
        return {
            "task_id": task_id,
            "resource_type": name,
            "resource_ids": [],
            "closed_ids": [],
            "unproven": [],
            "errors": [],
            "confirmed": True,
        }
    if hasattr(result, "closed_sessions"):
        remaining = tuple(getattr(result, "remaining_sessions", ()) or ())
        pending = tuple(getattr(result, "pending_runtime_cancellations", ()) or ())
        unproven = tuple(getattr(result, "unproven_process_kills", ()) or ())
        unproven_ids = [
            str(item.get("session_id") or item.get("resource_id") or "unknown") for item in unproven
        ]
        return {
            "task_id": task_id,
            "resource_type": name,
            "resource_ids": list(
                dict.fromkeys(
                    [str(item) for item in getattr(result, "closed_sessions", ())]
                    + [str(item) for item in remaining]
                    + unproven_ids
                )
            ),
            "closed_ids": [str(item) for item in getattr(result, "closed_sessions", ())],
            "unproven": [dict(item) for item in unproven]
            + [{"session_id": item, "kind": "remaining"} for item in remaining]
            + [{"runtime": item, "kind": "pending_cancellation"} for item in pending],
            "errors": [],
            "confirmed": bool(getattr(result, "confirmed", False)) and not unproven,
        }
    evidence = dict(getattr(result, "__dict__", {}) or {})
    evidence.setdefault("task_id", task_id)
    evidence.setdefault("resource_type", name)
    evidence.setdefault("resource_ids", [])
    evidence.setdefault("closed_ids", [])
    evidence.setdefault("unproven", [])
    evidence.setdefault("errors", [])
    evidence["confirmed"] = bool(getattr(result, "confirmed", False)) and not (
        evidence["unproven"] or evidence["errors"]
    )
    return evidence


def _timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _last_error(evidence: dict[str, Any]) -> str:
    errors = evidence.get("errors") or evidence.get("unproven") or ()
    return str((errors[0] if errors else {}).get("error") or "resource teardown unproven")


def _unresolved_ids(evidence: dict[str, Any]) -> tuple[str, ...]:
    """Return only resource ids lacking proof, never ids already closed."""
    values: list[str] = []
    for item in (*evidence.get("unproven", ()), *evidence.get("errors", ())):
        if not isinstance(item, dict):
            continue
        value = (
            item.get("resource_id")
            or item.get("session_id")
            or item.get("runtime_session_id")
            or item.get("runtime")
            or item.get("pid")
        )
        if value is not None:
            values.append(str(value))
    if values:
        return tuple(dict.fromkeys(values))
    return tuple(str(value) for value in (evidence.get("resource_ids") or ("unknown",)))


__all__ = ["TaskResourceFinalizer"]
