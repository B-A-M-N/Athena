"""Single owner for task-scoped computational resource teardown."""

from __future__ import annotations

import asyncio
import logging
import inspect
from collections import deque
from dataclasses import dataclass
from typing import Any

from athena.protocol.events import EV, make_event
from athena.protocol.resources import TaskResourceCloseResult
from athena.protocol.tasks import FINAL_STATUSES, TaskStatus

_logger = logging.getLogger("athena.service.resources")


@dataclass(frozen=True)
class TaskResourceRetentionPolicy:
    """What happens to task-owned resources after a parked slot expires."""

    mode: str = "release"
    retain_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.mode not in {"retain", "checkpoint_and_release", "release"}:
            raise ValueError(
                "resource retention mode must be retain, checkpoint_and_release, or release"
            )
        if self.retain_seconds < 0:
            raise ValueError("resource retention retain_seconds must be non-negative")


class TaskResourceFinalizer:
    """Close task-owned runtimes and capability sessions after finalization.

    TaskManager deliberately runs observers after the result is durable. This
    coordinator is the one observer responsible for the resource boundary, so
    a capability cannot quietly invent a second task cleanup path.
    """

    def __init__(
        self,
        *,
        event_sink=None,
        history_limit: int = 256,
        retention_policy: TaskResourceRetentionPolicy | None = None,
    ) -> None:
        self._event_sink = event_sink
        self._obligation_store = None
        self._outcomes: deque[dict[str, Any]] = deque(maxlen=history_limit)
        self._inflight: set[str] = set()
        self._unresolved: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._durability_error: str | None = None
        self._retention_policy = retention_policy or TaskResourceRetentionPolicy()
        self._parked_release_tasks: dict[str, asyncio.Task] = {}
        self._checkpoint_manager: Any = None
        # ``quiesce`` is the pre-publication half of finalization.  A
        # successful quiesce is already a close proof; the post-commit
        # observer must publish that proof, not close every resource a second
        # time.  Keep a process-local guard as well for duplicate observer
        # delivery.  Restart recovery still re-proves ownership through the
        # durable obligation store.
        self._quiesced: dict[str, dict[str, Any]] = {}
        self._finalized: set[str] = set()

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
        if task_id in self._finalized:
            return
        if task_id in self._inflight:
            return
        self._inflight.add(task_id)
        try:
            outcome = self._quiesced.get(task_id)
            if outcome is None or not outcome.get("confirmed"):
                outcome = await self._close_resources(task_id, status)
            self._outcomes.append(outcome)
            event_type = (
                EV["TASK_RESOURCES_FINALIZED"]
                if outcome["confirmed"]
                else EV["TASK_RESOURCE_TEARDOWN_FAILED"]
            )
            await self._emit(event_type, task_id, outcome)
            if outcome["confirmed"]:
                self._finalized.add(task_id)
                self._quiesced.pop(task_id, None)
        finally:
            self._inflight.discard(task_id)

    async def retry(self, task, result) -> None:
        """Retry only unresolved resource obligations for a terminal task."""
        await self.finalize(task, result)

    async def quiesce(self, task, result) -> dict[str, Any]:
        """Close task-owned resources before terminal publication.

        This is the pre-publication half of the finalization barrier. The
        ordinary ``finalize`` observer remains responsible for emitting the
        durable teardown event after the task result is committed.
        """
        status = getattr(result, "status", None)
        if status not in FINAL_STATUSES and status is not TaskStatus.RECOVERY_REQUIRED:
            return {"confirmed": True, "unresolved": []}
        task_id = str(task.id)
        existing = self._quiesced.get(task_id)
        outcome = existing if existing is not None and existing.get("confirmed") else None
        if outcome is None:
            outcome = await self._close_resources(task_id, status)
            self._quiesced[task_id] = outcome
        return {
            "confirmed": bool(outcome["confirmed"]),
            "unresolved": list(outcome["unresolved"]),
            "failures": list(outcome["failures"]),
        }

    async def release_parked(self, task_id: str) -> dict[str, Any]:
        """Apply the explicit parked-resource policy after slot release."""
        task_id = str(task_id)
        checkpoint: dict[str, Any] | None = None
        if self._retention_policy.mode == "checkpoint_and_release":
            checkpoint = await self._checkpoint_parked_task(task_id)
            if checkpoint is None:
                outcome = {
                    "task_id": task_id,
                    "mode": "checkpoint_and_release",
                    "released": False,
                    "retained": True,
                    "confirmed": False,
                    "error": "workspace checkpoint could not be established",
                }
                await self._emit("TaskResourceCheckpointFailed", task_id, outcome)
                return outcome
        if self._retention_policy.mode == "retain" and self._retention_policy.retain_seconds > 0:
            current = self._parked_release_tasks.get(task_id)
            if current is None or current.done():
                self._parked_release_tasks[task_id] = asyncio.create_task(
                    self._release_after_retention(task_id),
                    name=f"athena-parked-release-{task_id}",
                )
            outcome = {
                "task_id": task_id,
                "mode": "retain",
                "released": False,
                "retained": True,
                "retain_seconds": self._retention_policy.retain_seconds,
                "confirmed": True,
            }
        else:
            outcome = await self._close_resources(task_id, TaskStatus.WAITING_INPUT)
            outcome["mode"] = self._retention_policy.mode
            outcome["released"] = bool(outcome.get("closed"))
        if checkpoint is not None:
            outcome["checkpoint"] = checkpoint
            outcome["resume_consequence"] = (
                "task-owned runtimes were released; resume must reconstruct them from the checkpoint"
            )
        await self._emit("TaskResourceReleasedWhileParked", task_id, outcome)
        return outcome

    async def cancel_parked_release(self, task_id: str) -> None:
        """Cancel a delayed release when a parked task resumes in time."""
        task_id = str(task_id)
        pending = self._parked_release_tasks.pop(task_id, None)
        if pending is None or pending.done():
            return
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)

    async def shutdown(self) -> None:
        """Stop delayed retention jobs before service-owned resources close."""
        pending = list(self._parked_release_tasks.values())
        self._parked_release_tasks.clear()
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _release_after_retention(self, task_id: str) -> None:
        try:
            await asyncio.sleep(self._retention_policy.retain_seconds)
            outcome = await self._close_resources(task_id, TaskStatus.WAITING_INPUT)
            outcome.update(
                {
                    "mode": "retain_expired",
                    "released": bool(outcome.get("closed")),
                    "retained": False,
                }
            )
            await self._emit("TaskResourceReleasedWhileParked", task_id, outcome)
        except asyncio.CancelledError:
            raise
        finally:
            self._parked_release_tasks.pop(task_id, None)

    def bind_service(self, service: Any) -> None:
        self._service = service
        self._checkpoint_manager = getattr(service, "_checkpoints", None)

    def bind_checkpoint_manager(self, manager: Any) -> None:
        self._checkpoint_manager = manager

    async def _checkpoint_parked_task(self, task_id: str) -> dict[str, Any] | None:
        manager = self._checkpoint_manager
        service = getattr(self, "_service", None)
        task_manager = getattr(service, "_task_manager", None)
        if manager is None or task_manager is None:
            return None
        try:
            task = await task_manager.get(task_id)
            workspace = getattr(task, "workspace", None)
            root = getattr(workspace, "root", None)
            if not root:
                return None
            manifest = await manager.capture(
                task_id=task_id,
                workspace_root=str(root),
                label="parked-resource-release",
                metadata={
                    "reason": "parked resource retention policy",
                    "task_status": TaskStatus.WAITING_INPUT.value,
                },
            )
            return {
                "id": str(manifest.get("id") or ""),
                "workspace_root": str(root),
                "file_count": int(manifest.get("file_count") or 0),
            }
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
            _logger.warning("parked checkpoint failed for %s: %s", task_id, exc)
            return None

    def bind_obligation_store(self, store: Any) -> None:
        self._obligation_store = store

    async def load_unresolved(self) -> int:
        """Rehydrate open ownership before any worker can claim work."""
        if self._obligation_store is None:
            return 0
        records = await self._obligation_store.list_open()
        for record in records:
            key = (
                str(record["task_id"]),
                str(record["resource_type"]),
                str(record["resource_id"]),
            )
            self._unresolved[key] = {
                "task_id": key[0],
                "resource_type": key[1],
                "resource_id": key[2],
                "first_failure": record.get("first_failed_at"),
                "last_retry": record.get("last_attempt_at"),
                "attempts": int(record.get("attempt_count") or 0),
                "proof_state": str(record.get("state") or "UNPROVEN").casefold(),
                "last_error": record.get("last_error"),
                "ownership_identity": record.get("ownership_identity"),
                "evidence": record.get("proof") or {},
            }
        return len(records)

    async def reconcile_unresolved(self) -> int:
        """Give live resource managers a typed chance to prove closure."""
        resolved = 0
        for item in list(self._unresolved.values()):
            resource = self._resource_for(item["resource_type"])
            reconcile = getattr(resource, "reconcile", None) if resource is not None else None
            if not callable(reconcile):
                continue
            try:
                raw = reconcile(dict(item))
                result = await raw if inspect.isawaitable(raw) else raw
                evidence = _close_evidence(item["resource_type"], item["task_id"], result)
                if evidence.get("confirmed"):
                    if await self._clear_unresolved(
                        item["task_id"], item["resource_type"], (item["resource_id"],), evidence
                    ):
                        resolved += 1
                else:
                    await self._record_unresolved(item["task_id"], item["resource_type"], evidence)
            except Exception as exc:  # startup recovery remains fail-closed
                _logger.warning(
                    "resource obligation reconcile failed for %s/%s: %s",
                    item["resource_type"],
                    item["resource_id"],
                    exc,
                )
        return resolved

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
            "durability_error": self._durability_error,
        }

    def unresolved_for_task(self, task_id: str) -> list[dict[str, Any]]:
        """Return unresolved ownership proofs for one task."""
        return self._unresolved_for_task(str(task_id))

    async def _close_resources(self, task_id: str, status: Any) -> dict[str, Any]:
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
        service = getattr(self, "_service", None)
        resources = (
            ("debugger", getattr(service, "_debugger", None)),
            ("terminal", getattr(service, "_terminals", None)),
            ("browser", getattr(service, "_browser", None)),
            ("external_delegate", getattr(service, "_external_delegate_manager", None)),
            ("generated_runtime", getattr(service, "_synthesis", None)),
            ("execution", getattr(service, "_execution", None)),
        )
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
                    if not await self._clear_unresolved(task_id, name, (), evidence):
                        outcome["failures"].append(
                            {"resource": name, "error": "close proof could not be persisted"}
                        )
                else:
                    outcome["failures"].append(
                        {"resource": name, "error": "resource close was not confirmed"}
                    )
                    await self._record_unresolved(task_id, name, evidence)
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
                await self._record_unresolved(task_id, name, evidence)
                _logger.warning("task %s %s cleanup failed: %s", task_id, name, exc)
        outcome["unresolved"] = self._unresolved_for_task(task_id)
        outcome["confirmed"] = not outcome["unresolved"] and not self._durability_error
        return outcome

    def _resource_for(self, resource_type: str) -> Any:
        service = getattr(self, "_service", None)
        return {
            "debugger": getattr(service, "_debugger", None),
            "terminal": getattr(service, "_terminals", None),
            "browser": getattr(service, "_browser", None),
            "external_delegate": getattr(service, "_external_delegate_manager", None),
            "generated_runtime": getattr(service, "_synthesis", None),
            "execution": getattr(service, "_execution", None),
        }.get(resource_type)

    async def _record_unresolved(
        self, task_id: str, resource: str, evidence: dict[str, Any]
    ) -> None:
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
            if self._obligation_store is not None:
                try:
                    await self._obligation_store.record_failure(
                        task_id=task_id,
                        resource_type=resource,
                        resource_id=str(resource_id),
                        ownership_identity=_ownership_identity(evidence),
                        error=_last_error(evidence),
                        proof=evidence,
                    )
                except Exception as exc:
                    self._durability_error = str(exc)
                    _logger.error(
                        "resource obligation for %s/%s could not be persisted: %s",
                        task_id,
                        resource_id,
                        exc,
                    )
                else:
                    self._durability_error = None

    async def _clear_unresolved(
        self,
        task_id: str,
        resource: str,
        resource_ids: tuple[str, ...] = (),
        proof: dict[str, Any] | None = None,
    ) -> bool:
        if self._obligation_store is not None:
            try:
                await self._obligation_store.mark_closed(
                    task_id=task_id,
                    resource_type=resource,
                    resource_ids=resource_ids,
                    proof=proof,
                )
            except Exception as exc:
                self._durability_error = str(exc)
                _logger.error(
                    "resource closure proof for %s/%s could not be persisted: %s",
                    task_id,
                    resource,
                    exc,
                )
                return False
            else:
                self._durability_error = None
        for key in list(self._unresolved):
            if key[:2] == (task_id, resource) and (not resource_ids or key[2] in set(resource_ids)):
                self._unresolved.pop(key, None)
        return True

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


def _ownership_identity(evidence: dict[str, Any]) -> dict[str, Any]:
    """Extract a PID-safe ownership tuple for operator reconciliation.

    A bare PID is never sufficient: Linux can recycle it. Resource managers
    may provide process start identity, process group, cgroup, or runtime
    session identity in the structured proof.
    """
    fields = {
        "pid",
        "process_start_identity",
        "pgid",
        "cgroup",
        "runtime_session_id",
        "session_id",
    }
    identity: dict[str, Any] = {}
    for item in (*evidence.get("unproven", ()), *evidence.get("errors", ())):
        if not isinstance(item, dict):
            continue
        for field in fields:
            if item.get(field) is not None:
                identity[field] = item[field]
    return identity


__all__ = ["TaskResourceFinalizer", "TaskResourceRetentionPolicy"]
