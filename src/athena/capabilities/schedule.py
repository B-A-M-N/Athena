"""``schedule`` capability — exposes the scheduler to the model.

Operations:
    create  -> create a new scheduled job
    list    -> list all jobs
    inspect -> get details of one job
    enable  -> enable a job
    disable -> disable a job
    delete  -> delete a job
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.scheduler.scheduler import TriggerSpec, TriggerType
from athena.scheduler.triggers import next_fire
from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow


def _small_delta() -> timedelta:
    return timedelta(microseconds=1)


def _parse_datetime(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO-8601 datetime") from exc
    raise ValueError(f"{field} must be an ISO-8601 datetime")


def _trigger_from_metadata(job: Mapping[str, Any]) -> dict[str, Any]:
    metadata = job.get("metadata")
    if isinstance(metadata, dict):
        trigger = metadata.get("_trigger_spec")
        if isinstance(trigger, dict):
            return dict(trigger)
    return {}


def _owner_visible(job: Mapping[str, Any], owner: Mapping[str, str | None] | None) -> bool:
    """Check task/session/project ownership, while preserving legacy jobs."""
    metadata = job.get("metadata")
    stored = metadata.get("_owner") if isinstance(metadata, dict) else None
    if not isinstance(stored, dict) or not stored:
        return True
    if owner is None:
        return True
    return any(
        value and stored.get(key) == value
        for key, value in dict(owner).items()
        if key in {"task_id", "session_id", "project_id"}
    )


def _criterion_record(criterion: Any) -> dict[str, Any]:
    verification = getattr(criterion, "verification", None)
    return {
        "id": str(getattr(criterion, "id", "")),
        "description": str(getattr(criterion, "description", "")),
        "required": bool(getattr(criterion, "required", True)),
        "verification": None
        if verification is None
        else {
            "type": getattr(
                getattr(verification, "type", None),
                "value",
                getattr(verification, "type", "manual"),
            ),
            "command": getattr(verification, "command", None),
            "path": getattr(verification, "path", None),
            "predicate": getattr(verification, "predicate", None),
            "capability": getattr(verification, "capability", None),
        },
    }


class ScheduleAPI:
    """Thin interface the capability wraps."""

    def __init__(self, scheduler, task_manager) -> None:
        self._scheduler = scheduler
        self._task_manager = task_manager

    async def create(
        self,
        *,
        name: str,
        objective: str,
        trigger: dict,
        session_id: str | None = None,
        workspace_root: str | None = None,
        workspace=None,
        acceptance_criteria: tuple[Any, ...] = (),
        owner: Mapping[str, str | None] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict:
        job_id = new_id("job")
        trigger_spec = self._parse_trigger(trigger)
        owner_data = {key: value for key, value in dict(owner or {}).items() if value}
        now = utcnow()
        if trigger_spec.type is TriggerType.INTERVAL and trigger_spec.at is None:
            trigger_spec = replace(trigger_spec, at=now)
        first_run = trigger_spec.at
        if first_run is None and trigger_spec.type is not TriggerType.EVENT:
            first_run = next_fire(trigger_spec, now - _small_delta())
        await self._scheduler._store.upsert_job(
            job_id,
            name,
            payload={
                "template": {
                    "objective": objective,
                    "session_id": session_id,
                    "workspace_id": owner_data.get("project_id"),
                    "workspace_root": workspace_root,
                    "network_policy": getattr(
                        getattr(workspace, "network_policy", None),
                        "value",
                        getattr(workspace, "network_policy", None),
                    ),
                    "mutation_mode": getattr(
                        getattr(workspace, "mutation_mode", None),
                        "value",
                        getattr(workspace, "mutation_mode", None),
                    ),
                    "acceptance_criteria": [
                        _criterion_record(criterion) for criterion in acceptance_criteria
                    ],
                    "metadata": dict(metadata or {}),
                }
            },
            trigger_spec=self._scheduler_trigger_spec(trigger_spec),
            enabled=True,
            next_run=first_run.isoformat() if first_run else None,
            metadata={"_owner": owner_data},
        )
        return {"job_id": job_id, "name": name, "enabled": True}

    async def list_jobs(self, *, owner: Mapping[str, str | None] | None = None) -> list[dict]:
        jobs = await self._scheduler._store.list_jobs(enabled_only=False)
        return [self._public_job(job) for job in jobs if _owner_visible(job, owner)]

    async def inspect(
        self, job_id: str, *, owner: Mapping[str, str | None] | None = None
    ) -> dict | None:
        job = await self._scheduler._store.get_job_id(job_id)
        if job is None or not _owner_visible(job, owner):
            return None
        return self._public_job(job)

    async def enable(self, job_id: str, *, owner: Mapping[str, str | None] | None = None) -> bool:
        return await self._set_enabled(job_id, True, owner=owner)

    async def disable(self, job_id: str, *, owner: Mapping[str, str | None] | None = None) -> bool:
        return await self._set_enabled(job_id, False, owner=owner)

    async def delete(self, job_id: str, *, owner: Mapping[str, str | None] | None = None) -> bool:
        job = await self._scheduler._store.get_job_id(job_id)
        if job is None or not _owner_visible(job, owner):
            return False
        return await self._scheduler._store.delete_job(job_id)

    async def _set_enabled(self, job_id: str, enabled: bool, *, owner) -> bool:
        job = await self._scheduler._store.get_job_id(job_id)
        if job is None or not _owner_visible(job, owner):
            return False
        return await self._scheduler._store.set_enabled(job_id, enabled)

    @staticmethod
    def _public_job(job: Mapping[str, Any]) -> dict[str, Any]:
        trigger = _trigger_from_metadata(job)
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        template = payload.get("template") if isinstance(payload, dict) else {}
        return {
            "id": job["id"],
            "name": job["name"],
            "enabled": bool(job.get("enabled", True)),
            "next_run": job.get("next_run"),
            "last_run": job.get("last_run"),
            "trigger": trigger,
            "template": dict(template or {}),
            "metadata": dict(job.get("metadata") or {}),
        }

    def _parse_trigger(self, trigger: dict) -> TriggerSpec:
        """Parse a trigger dict into a TriggerSpec."""
        if not isinstance(trigger, dict):
            raise ValueError("trigger must be an object")
        allowed = {
            "type",
            "at",
            "interval_seconds",
            "cron",
            "event_name",
            "event_filters",
            "timezone",
            "end_at",
            "times",
            "metadata",
        }
        unknown = set(trigger) - allowed
        if unknown:
            raise ValueError(f"unknown trigger fields: {sorted(unknown)}")
        ttype = trigger.get("type")
        if ttype is None:
            raise ValueError("trigger.type is required")
        try:
            trigger_type = TriggerType(ttype)
        except ValueError as exc:
            raise ValueError(f"unknown trigger type: {ttype}") from exc
        at = None
        if "at" in trigger:
            at = _parse_datetime(trigger["at"], "trigger.at")
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
        interval = trigger.get("interval_seconds")
        if interval is not None:
            interval = float(interval)
            if interval <= 0:
                raise ValueError("trigger.interval_seconds must be positive")
        cron = trigger.get("cron")
        if trigger_type is TriggerType.CRON:
            if not isinstance(cron, str) or len(cron.split()) != 5:
                raise ValueError("cron trigger requires five fields")
        if trigger_type is TriggerType.EVENT and not trigger.get("event_name"):
            raise ValueError("event trigger requires event_name")
        if trigger_type is TriggerType.ONCE and at is None:
            raise ValueError("once trigger requires at")
        times = trigger.get("times")
        if times is not None and (
            not isinstance(times, int) or isinstance(times, bool) or times < 1
        ):
            raise ValueError("trigger.times must be a positive integer")
        timezone_name = trigger.get("timezone", "UTC")
        try:
            ZoneInfo(str(timezone_name))
        except Exception as exc:
            raise ValueError(f"invalid trigger timezone: {timezone_name}") from exc
        end_at = (
            _parse_datetime(trigger["end_at"], "trigger.end_at") if trigger.get("end_at") else None
        )
        if end_at is not None and end_at.tzinfo is None:
            end_at = end_at.replace(tzinfo=timezone.utc)
        return TriggerSpec(
            type=trigger_type,
            at=at,
            interval_seconds=interval,
            cron=cron,
            event_name=trigger.get("event_name"),
            event_filters=dict(trigger.get("event_filters") or {}),
            timezone=str(timezone_name),
            end_at=end_at,
            times=times,
            metadata=dict(trigger.get("metadata") or {}),
        )

    def _scheduler_trigger_spec(self, spec: TriggerSpec) -> dict[str, Any]:
        return {
            "type": spec.type.value,
            "at": spec.at.isoformat() if spec.at else None,
            "interval_seconds": spec.interval_seconds,
            "cron": spec.cron,
            "event_name": spec.event_name,
            "event_filters": dict(spec.event_filters),
            "timezone": spec.timezone,
            "end_at": spec.end_at.isoformat() if spec.end_at else None,
            "times": spec.times,
            "metadata": dict(spec.metadata),
        }


class ScheduleCapability:
    """Expose scheduling as a capability (operations: create/list/inspect/enable/disable/delete)."""

    descriptor = CapabilityDescriptor(
        id="schedule",
        description="Create, list, inspect, enable, disable, and delete scheduled jobs.",
        input_schema={
            "type": "object",
            "required": ["operation"],
            "additionalProperties": False,
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["create", "list", "inspect", "enable", "disable", "delete"],
                },
                "job_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "name": {"type": "string", "minLength": 1, "maxLength": 256},
                "objective": {"type": "string", "minLength": 1, "maxLength": 10000},
                "session_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "workspace_root": {"type": "string", "minLength": 1, "maxLength": 4096},
                "trigger": {"type": "object", "maxProperties": 16},
            },
            "oneOf": [
                {
                    "properties": {"operation": {"const": "create"}},
                    "required": ["name", "objective", "trigger"],
                },
                {
                    "properties": {
                        "operation": {"enum": ["inspect", "enable", "disable", "delete"]}
                    },
                    "required": ["job_id"],
                },
                {"properties": {"operation": {"const": "list"}}},
            ],
        },
        effects=frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, api: ScheduleAPI) -> None:
        self._api = api

    async def invoke(
        self, request: CapabilityRequest, *, output_accumulator=None, context=None
    ) -> CapabilityResult:
        args = dict(request.arguments or {})
        op = args.get("operation", "")
        call_id = request.call_id or new_id("call")
        owner = {
            "task_id": request.task_id,
            "session_id": request.session_id,
            "project_id": getattr(getattr(context, "workspace", None), "id", None),
        }
        try:
            if op == "create":
                requested_session = args.get("session_id")
                if (
                    requested_session
                    and request.session_id
                    and requested_session != request.session_id
                    and request.origin.value == "model"
                ):
                    return CapabilityResult(
                        call_id,
                        self.descriptor.id,
                        CapabilityResultStatus.FAILED,
                        error="model cannot schedule work for another session",
                    )
                workspace = getattr(context, "workspace", None)
                requested_root = args.get("workspace_root")
                if workspace is None and requested_root:
                    return CapabilityResult(
                        call_id,
                        self.descriptor.id,
                        CapabilityResultStatus.FAILED,
                        error="workspace context is required for workspace_root",
                    )
                workspace_root = (
                    str(requested_root) if requested_root else getattr(workspace, "root", None)
                )
                if workspace is not None and workspace_root:
                    base = os.path.realpath(str(workspace.root))
                    resolved = os.path.realpath(workspace_root)
                    if resolved != base and not resolved.startswith(base + os.sep):
                        return CapabilityResult(
                            call_id,
                            self.descriptor.id,
                            CapabilityResultStatus.FAILED,
                            error="scheduled workspace must remain within current workspace",
                        )
                result = await self._api.create(
                    name=args.get("name", "scheduled task"),
                    objective=args.get("objective", ""),
                    trigger=args.get("trigger", {}),
                    session_id=requested_session or request.session_id,
                    workspace_root=workspace_root,
                    workspace=workspace,
                    owner=owner,
                )
                return CapabilityResult(
                    call_id,
                    self.descriptor.id,
                    CapabilityResultStatus.OK,
                    output=json.dumps(result),
                    metadata={"operation": "create"},
                )
            elif op == "list":
                jobs = await self._api.list_jobs(owner=owner)
                return CapabilityResult(
                    call_id,
                    self.descriptor.id,
                    CapabilityResultStatus.OK,
                    output=json.dumps(jobs),
                    metadata={"operation": "list"},
                )
            elif op == "inspect":
                job = await self._api.inspect(args.get("job_id", ""), owner=owner)
                if job is None:
                    return CapabilityResult(
                        call_id,
                        self.descriptor.id,
                        CapabilityResultStatus.FAILED,
                        error="job not found",
                    )
                return CapabilityResult(
                    call_id,
                    self.descriptor.id,
                    CapabilityResultStatus.OK,
                    output=json.dumps(job),
                    metadata={"operation": "inspect"},
                )
            elif op == "enable":
                ok = await self._api.enable(args.get("job_id", ""), owner=owner)
                if not ok:
                    return CapabilityResult(
                        call_id,
                        self.descriptor.id,
                        CapabilityResultStatus.FAILED,
                        error="job not found or not owned",
                    )
                return CapabilityResult(
                    call_id,
                    self.descriptor.id,
                    CapabilityResultStatus.OK,
                    output=json.dumps({"enabled": ok}),
                    metadata={"operation": "enable"},
                )
            elif op == "disable":
                ok = await self._api.disable(args.get("job_id", ""), owner=owner)
                if not ok:
                    return CapabilityResult(
                        call_id,
                        self.descriptor.id,
                        CapabilityResultStatus.FAILED,
                        error="job not found or not owned",
                    )
                return CapabilityResult(
                    call_id,
                    self.descriptor.id,
                    CapabilityResultStatus.OK,
                    output=json.dumps({"enabled": ok}),
                    metadata={"operation": "disable"},
                )
            elif op == "delete":
                ok = await self._api.delete(args.get("job_id", ""), owner=owner)
                if not ok:
                    return CapabilityResult(
                        call_id,
                        self.descriptor.id,
                        CapabilityResultStatus.FAILED,
                        error="job not found or not owned",
                    )
                return CapabilityResult(
                    call_id,
                    self.descriptor.id,
                    CapabilityResultStatus.OK,
                    output=json.dumps({"deleted": ok}),
                    metadata={"operation": "delete"},
                )
            return CapabilityResult(
                call_id,
                self.descriptor.id,
                CapabilityResultStatus.FAILED,
                error=f"unknown operation: {op}",
            )
        except Exception as exc:
            return CapabilityResult(
                call_id,
                self.descriptor.id,
                CapabilityResultStatus.FAILED,
                error=f"schedule.{op} failed: {exc}",
            )
