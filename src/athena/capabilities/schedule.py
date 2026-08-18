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
from typing import Any

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.scheduler.scheduler import TriggerSpec, TriggerType
from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from datetime import datetime, timezone


class ScheduleAPI:
    """Thin interface the capability wraps."""

    def __init__(self, scheduler, task_manager) -> None:
        self._scheduler = scheduler
        self._task_manager = task_manager

    async def create(self, *, name: str, objective: str, trigger: dict,
                     session_id: str | None = None) -> dict:
        job_id = new_id("job")
        trigger_spec = self._parse_trigger(trigger)
        await self._scheduler._store.upsert_job(
            job_id, name,
            payload={"template": {"objective": objective, "session_id": session_id}},
            trigger_spec=self._scheduler_trigger_spec(trigger_spec),
            enabled=True,
            next_run=trigger_spec.at.isoformat() if trigger_spec.at else utcnow().isoformat(),
        )
        return {"job_id": job_id, "name": name, "enabled": True}

    async def list_jobs(self) -> list[dict]:
        jobs = await self._scheduler._store.list_jobs()
        return [{"id": j["id"], "name": j["name"], "enabled": j.get("enabled", True)} for j in jobs]

    async def inspect(self, job_id: str) -> dict | None:
        job = await self._scheduler._store.get_job_id(job_id)
        if job is None:
            return None
        return {"id": job["id"], "name": job["name"], "enabled": job.get("enabled", True)}

    async def enable(self, job_id: str) -> bool:
        return await self._scheduler._store.set_enabled(job_id, True)

    async def disable(self, job_id: str) -> bool:
        return await self._scheduler._store.set_enabled(job_id, False)

    async def delete(self, job_id: str) -> bool:
        return await self._scheduler._store.delete_job(job_id)

    def _parse_trigger(self, trigger: dict) -> TriggerSpec:
        """Parse a trigger dict into a TriggerSpec."""
        ttype = trigger.get("type", "once")
        at = None
        if "at" in trigger:
            at = datetime.fromisoformat(trigger["at"]) if isinstance(trigger["at"], str) else trigger["at"]
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
        interval = trigger.get("interval_seconds")
        cron = trigger.get("cron")
        return TriggerSpec(
            type=TriggerType(ttype),
            at=at,
            interval_seconds=interval,
            cron=cron,
            timezone=trigger.get("timezone", "UTC"),
        )

    def _scheduler_trigger_spec(self, spec: TriggerSpec) -> dict[str, Any]:
        return {
            "type": spec.type.value,
            "at": spec.at.isoformat() if spec.at else None,
            "interval_seconds": spec.interval_seconds,
            "cron": spec.cron,
            "timezone": spec.timezone,
        }


class ScheduleCapability:
    """Expose scheduling as a capability (operations: create/list/inspect/enable/disable/delete)."""

    descriptor = CapabilityDescriptor(
        id="schedule",
        description="Create, list, inspect, enable, disable, and delete scheduled jobs.",
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {"type": "string", "enum": ["create", "list", "inspect", "enable", "disable", "delete"]},
                "job_id": {"type": "string"},
                "name": {"type": "string"},
                "objective": {"type": "string"},
                "trigger": {"type": "object"},
            },
        },
        effects=frozenset({EffectClass.WRITE_LOCAL}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, api: ScheduleAPI) -> None:
        self._api = api

    async def invoke(self, request: CapabilityRequest, *, output_accumulator=None, context=None) -> CapabilityResult:
        args = dict(request.arguments or {})
        op = args.get("operation", "")
        call_id = request.call_id or new_id("call")
        try:
            if op == "create":
                result = await self._api.create(
                    name=args.get("name", "scheduled task"),
                    objective=args.get("objective", ""),
                    trigger=args.get("trigger", {"type": "once"}),
                    session_id=args.get("session_id"),
                )
                return CapabilityResult(call_id, self.descriptor.id, CapabilityResultStatus.OK,
                                       output=json.dumps(result), metadata={"operation": "create"})
            elif op == "list":
                jobs = await self._api.list_jobs()
                return CapabilityResult(call_id, self.descriptor.id, CapabilityResultStatus.OK,
                                       output=json.dumps(jobs), metadata={"operation": "list"})
            elif op == "inspect":
                job = await self._api.inspect(args.get("job_id", ""))
                if job is None:
                    return CapabilityResult(call_id, self.descriptor.id, CapabilityResultStatus.FAILED,
                                           error="job not found")
                return CapabilityResult(call_id, self.descriptor.id, CapabilityResultStatus.OK,
                                       output=json.dumps(job), metadata={"operation": "inspect"})
            elif op == "enable":
                ok = await self._api.enable(args.get("job_id", ""))
                return CapabilityResult(call_id, self.descriptor.id, CapabilityResultStatus.OK,
                                       output=json.dumps({"enabled": ok}), metadata={"operation": "enable"})
            elif op == "disable":
                ok = await self._api.disable(args.get("job_id", ""))
                return CapabilityResult(call_id, self.descriptor.id, CapabilityResultStatus.OK,
                                       output=json.dumps({"enabled": ok}), metadata={"operation": "disable"})
            elif op == "delete":
                ok = await self._api.delete(args.get("job_id", ""))
                return CapabilityResult(call_id, self.descriptor.id, CapabilityResultStatus.OK,
                                       output=json.dumps({"deleted": ok}), metadata={"operation": "delete"})
            return CapabilityResult(call_id, self.descriptor.id, CapabilityResultStatus.FAILED,
                                   error=f"unknown operation: {op}")
        except Exception as exc:
            return CapabilityResult(call_id, self.descriptor.id, CapabilityResultStatus.FAILED,
                                   error=f"schedule.{op} failed: {exc}")
