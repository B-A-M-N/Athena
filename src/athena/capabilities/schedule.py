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
import hashlib
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
    ResourceClass,
)
from athena.scheduler.scheduler import TriggerSpec, TriggerType
from athena.scheduler.triggers import next_fire
from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from athena.protocol.tasks import DeliverySpec

_UNSET = object()


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
        if key in {"task_id", "session_id", "project_id", "principal_id"}
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


def _authority_snapshot(
    *,
    workspace: Any,
    capability_policy: Any,
    model_policy: Any,
    resource_budget: Any,
    autonomy: Any,
    delivery: DeliverySpec | None,
    owner: Mapping[str, Any],
) -> dict[str, Any]:
    """Serialize the service-owned ceiling captured at schedule creation."""
    workspace_record: dict[str, Any] = {}
    if workspace is not None:
        workspace_record = {
            "id": getattr(workspace, "id", None),
            "root": getattr(workspace, "root", None),
            "readable": [
                {"path": rule.path, "allow": rule.allow}
                for rule in getattr(workspace, "readable", ())
            ],
            "writable": [
                {"path": rule.path, "allow": rule.allow}
                for rule in getattr(workspace, "writable", ())
            ],
            "temp_root": getattr(workspace, "temp_root", None),
            "execution_backend": getattr(workspace, "execution_backend", None),
            "revision": getattr(workspace, "revision", None),
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
        }
    cp = capability_policy
    if cp is None:
        # Direct store/API callers have not presented a creator authority
        # snapshot; a persisted schedule must not silently become unrestricted.
        class _SafePolicy:
            effects = ()
            allow = ()
            ask = ()
            deny = ("*",)

        cp = _SafePolicy()
    mp = model_policy
    budget = resource_budget
    delivery_record = None
    delivery_effects: list[str] = []
    if delivery is not None:
        destination = str(delivery.destination or "")
        parts = urlsplit(destination)
        canonical = urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                parts.path or "/",
                parts.query,
                "",
            )
        )
        delivery_record = {
            "channel": delivery.channel,
            "destination": destination,
            "destination_hash": hashlib.sha256(canonical.encode()).hexdigest(),
            "credential_identity": None,
        }
        if str(delivery.channel or "").strip().lower() == "webhook":
            delivery_effects = [
                EffectClass.NETWORK_WRITE.value,
                EffectClass.EXTERNAL_MESSAGE.value,
            ]
    return {
        "principal": dict(owner),
        "workspace": workspace_record,
        "capability_policy": {
            "effects": sorted(
                str(getattr(value, "value", value)) for value in getattr(cp, "effects", ()) or ()
            ),
            "allow": list(getattr(cp, "allow", ()) or ()),
            "ask": list(getattr(cp, "ask", ()) or ()),
            "deny": list(getattr(cp, "deny", ()) or ()),
        },
        "model_policy": {
            "role": getattr(mp, "role", "primary"),
            "allowed": list(getattr(mp, "allowed", ()) or ()),
            "require_tools": bool(getattr(mp, "require_tools", False)),
            "privacy": getattr(mp, "privacy", "local-preferred"),
            "max_cost_usd": (
                str(getattr(mp, "max_cost_usd"))
                if getattr(mp, "max_cost_usd", None) is not None
                else None
            ),
            "routing_preference": getattr(mp, "routing_preference", "balanced"),
        },
        "resource_budget": {
            name: (
                value.total_seconds()
                if hasattr(value, "total_seconds")
                else str(value)
                if name == "max_cost_usd" and value is not None
                else value
            )
            for name in (
                "max_agent_iterations",
                "max_input_tokens",
                "max_output_tokens",
                "max_cost_usd",
                "max_wall_time",
                "max_children",
                "max_child_depth",
                "max_parallel_model_calls",
                "max_parallel_executions",
                "max_artifact_bytes",
            )
            if (value := getattr(budget, name, None)) is not None
        },
        "autonomy": getattr(autonomy, "value", autonomy) or "supervised",
        "delivery": delivery_record,
        "delivery_effects": delivery_effects,
        "effect_ceiling": list(delivery_effects),
    }


def _schedule_effects(arguments: Mapping[str, Any]) -> frozenset[EffectClass]:
    """Resolve schedule effects, including the future delivery grant."""
    operation = str(arguments.get("operation") or "").lower()
    effects = {EffectClass.READ_LOCAL}
    if operation in {"create", "update", "enable", "disable", "delete", "run"}:
        effects.add(EffectClass.WRITE_LOCAL)
    delivery = arguments.get("delivery")
    if operation in {"create", "update"} and isinstance(delivery, Mapping):
        if str(delivery.get("channel") or "").strip().lower() == "webhook":
            effects.update({EffectClass.NETWORK_WRITE, EffectClass.EXTERNAL_MESSAGE})
    return frozenset(effects)


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
        capability_policy=None,
        model_policy=None,
        resource_budget=None,
        autonomy=None,
        reuse_session: bool = False,
        delivery: DeliverySpec | None = None,
        continuity: str = "fresh",
    ) -> dict:
        if continuity not in {"fresh", "previous_result", "job_memory", "session"}:
            raise ValueError("continuity must be fresh, previous_result, job_memory, or session")
        # ``reuse_session`` is the legacy spelling of explicit session
        # continuity. Persist a schedule-owned identity rather than the
        # creator's generic session field.
        if reuse_session and continuity == "fresh":
            continuity = "session"
        continuity_session_id = session_id if continuity == "session" else None
        if continuity == "session" and continuity_session_id is None:
            continuity_session_id = new_id("session")
        job_id = new_id("job")
        trigger_spec = self._parse_trigger(trigger)
        owner_data = {key: value for key, value in dict(owner or {}).items() if value}
        now = utcnow()
        if trigger_spec.type is TriggerType.INTERVAL and trigger_spec.at is None:
            trigger_spec = replace(trigger_spec, at=now)
        first_run = trigger_spec.at
        if first_run is None and trigger_spec.type is not TriggerType.EVENT:
            first_run = next_fire(trigger_spec, now - _small_delta())
        authority = _authority_snapshot(
            workspace=workspace,
            capability_policy=capability_policy,
            model_policy=model_policy,
            resource_budget=resource_budget,
            autonomy=autonomy,
            delivery=delivery,
            owner=owner_data,
        )
        # Occurrences default to FRESH sessions: recurring autonomous work
        # must not accumulate history inside the conversation that scheduled
        # it, inherit stale user instructions, or grow unboundedly expensive.
        # The scheduling task/session are carried as lineage, not as the
        # execution session. A persistent shared session is an explicit
        # opt-in through ``reuse_session``.
        template_metadata = dict(metadata or {})
        template_metadata.setdefault(
            "_schedule_lineage",
            {
                "job_id": job_id,
                "continuity": continuity,
                "creator_task_id": owner_data.get("task_id"),
                "creator_session_id": owner_data.get("session_id"),
            },
        )
        await self._scheduler._store.upsert_job(
            job_id,
            name,
            payload={
                "template": {
                    "objective": objective,
                    "session_id": None,
                    "continuity_session_id": continuity_session_id,
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
                    "delivery": (
                        {"channel": delivery.channel, "destination": delivery.destination}
                        if delivery is not None
                        else None
                    ),
                    "continuity": continuity,
                    "metadata": template_metadata,
                }
            },
            trigger_spec=self._scheduler_trigger_spec(trigger_spec),
            enabled=True,
            next_run=first_run.isoformat() if first_run else None,
            metadata={"_owner": owner_data, "_authority_snapshot": authority},
        )
        return {"job_id": job_id, "name": name, "enabled": True}

    async def update(
        self,
        job_id: str,
        *,
        owner: Mapping[str, str | None] | None = None,
        objective: str | None = None,
        trigger: dict | None = None,
        enabled: bool | None = None,
        continuity: str | None = None,
        delivery: DeliverySpec | None | object = _UNSET,
    ) -> dict | None:
        """Update schedule-owned fields without widening its authority snapshot."""
        job = await self._scheduler._store.get_job_id(job_id)
        if job is None or not _owner_visible(job, owner):
            return None
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        template = dict(payload.get("template") or {})
        if objective is not None:
            value = str(objective).strip()
            if not value or len(value) > 10_000:
                raise ValueError("objective must contain 1-10000 characters")
            template["objective"] = value
        if continuity is not None:
            if continuity not in {"fresh", "previous_result", "job_memory", "session"}:
                raise ValueError(
                    "continuity must be fresh, previous_result, job_memory, or session"
                )
            template["continuity"] = continuity
            if continuity == "session" and not template.get("continuity_session_id"):
                template["continuity_session_id"] = new_id("session")
        if delivery is not _UNSET:
            if delivery is None:
                template["delivery"] = None
            else:
                if not isinstance(delivery, DeliverySpec):
                    raise TypeError("delivery must be a DeliverySpec or None")
                template["delivery"] = {
                    "channel": delivery.channel,
                    "destination": delivery.destination,
                }
        trigger_spec = _trigger_from_metadata(job)
        next_run = job.get("next_run")
        if trigger is not None:
            parsed = self._parse_trigger(trigger)
            if parsed.type is TriggerType.INTERVAL and parsed.at is None:
                parsed = replace(parsed, at=utcnow())
            trigger_spec = self._scheduler_trigger_spec(parsed)
            next_value = (
                None
                if parsed.type is TriggerType.EVENT
                else next_fire(parsed, utcnow() - _small_delta())
            )
            next_run = next_value.isoformat() if next_value is not None else None
        metadata = dict(job.get("metadata") or {})
        metadata["_trigger_spec"] = dict(trigger_spec)
        if delivery is not _UNSET:
            authority = dict(metadata.get("_authority_snapshot") or {})
            if delivery is None:
                authority["delivery"] = None
                authority["delivery_effects"] = []
                authority["effect_ceiling"] = []
            else:
                if not isinstance(delivery, DeliverySpec):
                    raise TypeError("delivery must be a DeliverySpec or None")
                new_delivery = delivery
                destination = str(new_delivery.destination or "")
                parts = urlsplit(destination)
                canonical = urlunsplit(
                    (
                        parts.scheme.lower(),
                        parts.netloc.lower(),
                        parts.path or "/",
                        parts.query,
                        "",
                    )
                )
                record = {
                    "channel": new_delivery.channel,
                    "destination": destination,
                    "destination_hash": hashlib.sha256(canonical.encode()).hexdigest(),
                    "credential_identity": None,
                }
                effects = (
                    [EffectClass.NETWORK_WRITE.value, EffectClass.EXTERNAL_MESSAGE.value]
                    if str(new_delivery.channel or "").strip().lower() == "webhook"
                    else []
                )
                authority["delivery"] = record
                authority["delivery_effects"] = effects
                authority["effect_ceiling"] = list(effects)
            metadata["_authority_snapshot"] = authority
        await self._scheduler._store.upsert_job(
            job_id,
            str(job.get("name") or template.get("objective") or job_id),
            payload={"template": template},
            trigger_spec=trigger_spec,
            enabled=bool(job.get("enabled", True)) if enabled is None else bool(enabled),
            next_run=next_run,
            metadata=metadata,
        )
        return await self.inspect(job_id, owner=owner)

    async def run(
        self, job_id: str, *, owner: Mapping[str, str | None] | None = None
    ) -> str | None:
        job = await self._scheduler._store.get_job_id(job_id)
        if job is None or not _owner_visible(job, owner):
            return None
        return await self._scheduler.run_now(job_id)

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
    """Expose scheduling as a bounded task-lifecycle capability."""

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
                    "enum": [
                        "create",
                        "list",
                        "inspect",
                        "enable",
                        "disable",
                        "delete",
                        "update",
                        "run",
                    ],
                },
                "job_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "name": {"type": "string", "minLength": 1, "maxLength": 256},
                "objective": {"type": "string", "minLength": 1, "maxLength": 10000},
                "session_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "workspace_root": {"type": "string", "minLength": 1, "maxLength": 4096},
                "persistent_session": {
                    "type": "boolean",
                    "description": (
                        "Opt in to running every occurrence in one persistent "
                        "session. Default false: each occurrence gets a fresh "
                        "session with lineage back to this schedule."
                    ),
                },
                "trigger": {"type": "object", "maxProperties": 16},
                "delivery": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string", "maxLength": 64},
                        "destination": {"type": "string", "maxLength": 4096},
                    },
                    "additionalProperties": False,
                },
                "enabled": {"type": "boolean"},
                "continuity": {
                    "type": "string",
                    "enum": ["fresh", "previous_result", "job_memory", "session"],
                },
            },
            "oneOf": [
                {
                    "properties": {"operation": {"const": "create"}},
                    "required": ["name", "objective", "trigger"],
                },
                {
                    "properties": {
                        "operation": {
                            "enum": [
                                "inspect",
                                "enable",
                                "disable",
                                "delete",
                                "update",
                                "run",
                            ]
                        }
                    },
                    "required": ["job_id"],
                },
                {"properties": {"operation": {"const": "list"}}},
            ],
        },
        effects=frozenset(
            {
                EffectClass.READ_LOCAL,
                EffectClass.WRITE_LOCAL,
                EffectClass.NETWORK_WRITE,
                EffectClass.EXTERNAL_MESSAGE,
            }
        ),
        effect_resolver=_schedule_effects,
        resources=frozenset({ResourceClass.SCHEDULE}),
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
            "principal_id": getattr(context, "principal_id", None),
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
                session_continuity = str(args.get("continuity") or "fresh") == "session"
                persistent_session = bool(args.get("persistent_session")) or session_continuity
                result = await self._api.create(
                    name=args.get("name", "scheduled task"),
                    objective=args.get("objective", ""),
                    trigger=args.get("trigger", {}),
                    # Fresh sessions per occurrence are the default: only an
                    # explicit persistent_session flag reuses the requesting
                    # session. The requesting task/session are carried as
                    # lineage in the template metadata, never as the
                    # execution session.
                    session_id=request.session_id if persistent_session else None,
                    reuse_session=persistent_session,
                    workspace_root=workspace_root,
                    workspace=workspace,
                    owner=owner,
                    capability_policy=getattr(context, "capability_policy", None),
                    model_policy=getattr(context, "model_policy", None),
                    resource_budget=getattr(context, "resource_budget", None),
                    autonomy=getattr(context, "autonomy", None),
                    delivery=_decode_delivery(args.get("delivery")),
                    continuity=str(args.get("continuity") or "fresh"),
                )
                job_id = (
                    result.get("id") or result.get("job_id") if isinstance(result, dict) else None
                )
                return CapabilityResult(
                    call_id,
                    self.descriptor.id,
                    CapabilityResultStatus.OK,
                    output=json.dumps(result),
                    metadata={
                        "operation": "create",
                        **({"mutation_ref": str(job_id)} if job_id else {}),
                    },
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
            elif op == "update":
                updated = await self._api.update(
                    args.get("job_id", ""),
                    owner=owner,
                    objective=args.get("objective"),
                    trigger=args.get("trigger"),
                    enabled=args.get("enabled"),
                    continuity=args.get("continuity"),
                    delivery=(
                        _decode_delivery(args.get("delivery")) if "delivery" in args else _UNSET
                    ),
                )
                if updated is None:
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
                    output=json.dumps(updated),
                    metadata={"operation": "update"},
                )
            elif op == "run":
                task_id = await self._api.run(args.get("job_id", ""), owner=owner)
                if task_id is None:
                    return CapabilityResult(
                        call_id,
                        self.descriptor.id,
                        CapabilityResultStatus.FAILED,
                        error="job not found, not owned, or disabled",
                    )
                return CapabilityResult(
                    call_id,
                    self.descriptor.id,
                    CapabilityResultStatus.OK,
                    output=json.dumps({"task_id": task_id}),
                    metadata={"operation": "run"},
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


def _decode_delivery(raw: Any) -> DeliverySpec | None:
    if not isinstance(raw, Mapping) or not raw.get("channel"):
        return None
    return DeliverySpec(
        channel=str(raw["channel"]),
        destination=(str(raw["destination"]) if raw.get("destination") is not None else None),
    )
