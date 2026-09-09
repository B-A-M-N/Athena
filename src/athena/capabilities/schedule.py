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
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from types import SimpleNamespace
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
from athena.network import validate_target
from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from athena.protocol.tasks import DeliverySpec

_UNSET = object()


@dataclass(frozen=True)
class ScheduleControl:
    """Current caller authority used to control a persisted schedule."""

    origin: str = "user_direct"
    task_id: str | None = None
    session_id: str | None = None
    principal_id: str | None = None
    project_id: str | None = None
    capability_policy: Any = None
    resource_budget: Any = None
    workspace: Any = None
    narrow_to_caller: bool = False


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


def _owner_visible(
    job: Mapping[str, Any],
    owner: Mapping[str, str | None] | None,
    *,
    control: ScheduleControl | None = None,
) -> bool:
    """Check visibility separately from mutation authority.

    Legacy jobs without an owner remain available to operator surfaces but are
    invisible to model-facing callers until explicitly adopted.
    """
    metadata = job.get("metadata")
    stored = metadata.get("_owner") if isinstance(metadata, dict) else None
    if not isinstance(stored, dict) or not stored:
        return control is None or control.origin != "model"
    if owner is None:
        return True
    if control is not None and control.origin == "model":
        scoped = {key: value for key, value in dict(owner).items() if value}
        return bool(scoped) and all(stored.get(key) == value for key, value in scoped.items())
    return any(
        value and stored.get(key) == value
        for key, value in dict(owner).items()
        if key in {"task_id", "session_id", "project_id", "principal_id"}
    )


def _authority_digest(authority: Mapping[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(dict(authority), sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _stored_control_grant(job: Mapping[str, Any]) -> Mapping[str, Any] | None:
    metadata = job.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    grant = metadata.get("_control_grant")
    return grant if isinstance(grant, Mapping) else None


def _policy_covers(stored: Mapping[str, Any], current: Any) -> bool:
    if current is None:
        return False
    stored_effects = {str(value) for value in stored.get("effects") or ()}
    current_effects = {
        str(getattr(value, "value", value)) for value in getattr(current, "effects", ()) or ()
    }
    if stored_effects and current_effects and not stored_effects.issubset(current_effects):
        return False
    if stored_effects and not current_effects and bool(getattr(current, "deny", ())):
        return False
    stored_allow = set(str(value) for value in stored.get("allow") or ())
    current_allow = set(str(value) for value in getattr(current, "allow", ()) or ())
    if stored_allow and current_allow and not stored_allow.issubset(current_allow):
        return False
    stored_deny = set(str(value) for value in stored.get("deny") or ())
    current_deny = set(str(value) for value in getattr(current, "deny", ()) or ())
    if "*" in stored_deny and "*" not in current_deny:
        return False
    return True


def _budget_covers(stored: Mapping[str, Any], current: Any) -> bool:
    if current is None:
        return False
    for name, stored_value in stored.items():
        if stored_value is None:
            continue
        current_value = getattr(current, name, None)
        if current_value is None:
            continue
        try:
            if float(current_value.total_seconds()) < float(stored_value):
                return False
        except AttributeError:
            if float(current_value) < float(stored_value):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _current_authority(
    control: ScheduleControl,
    *,
    owner: Mapping[str, Any],
) -> dict[str, Any]:
    return _authority_snapshot(
        workspace=control.workspace,
        capability_policy=control.capability_policy,
        model_policy=None,
        resource_budget=control.resource_budget,
        autonomy=None,
        delivery=None,
        owner=owner,
    )


def _authority_covers(
    job: Mapping[str, Any],
    owner: Mapping[str, str | None] | None,
    control: ScheduleControl | None,
) -> bool:
    if control is None or control.origin in {"user_direct", "trusted_orchestration", "system"}:
        return True
    if control.origin != "model" or owner is None:
        return False
    grant = _stored_control_grant(job)
    if grant is None:
        return False
    if grant.get("creator_task_id") and grant.get("creator_task_id") != control.task_id:
        return False
    if grant.get("creator_session_id") and grant.get("creator_session_id") != control.session_id:
        return False
    if grant.get("principal_id") and grant.get("principal_id") != control.principal_id:
        return False
    metadata = job.get("metadata")
    stored_authority = (
        metadata.get("_authority_snapshot")
        if isinstance(metadata, Mapping)
        else None
    )
    if not isinstance(stored_authority, Mapping):
        return False
    if grant.get("authority_digest") != stored_authority.get("authority_digest"):
        return False
    if not _policy_covers(stored_authority.get("capability_policy") or {}, control.capability_policy):
        return False
    if not _budget_covers(stored_authority.get("resource_budget") or {}, control.resource_budget):
        return False
    stored_workspace = stored_authority.get("workspace") or {}
    current_workspace = _current_authority(control, owner=owner).get("workspace") or {}
    stored_root = str(stored_workspace.get("root") or "")
    current_root = str(current_workspace.get("root") or "")
    return not stored_root or stored_root == current_root


def _intersect_authority(
    stored: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    """Narrow a schedule to the caller's current authority."""
    result = dict(stored)
    stored_policy = dict(stored.get("capability_policy") or {})
    current_policy = dict(current.get("capability_policy") or {})
    stored_effects = set(stored_policy.get("effects") or ())
    current_effects = set(current_policy.get("effects") or ())
    if current_effects:
        stored_policy["effects"] = sorted(stored_effects & current_effects) if stored_effects else sorted(current_effects)
    stored_policy["allow"] = sorted(
        set(stored_policy.get("allow") or ()) & set(current_policy.get("allow") or ())
    ) if current_policy.get("allow") else list(stored_policy.get("allow") or ())
    stored_policy["ask"] = sorted(
        set(stored_policy.get("ask") or ()) & set(current_policy.get("ask") or ())
    ) if current_policy.get("ask") else list(stored_policy.get("ask") or ())
    stored_policy["deny"] = sorted(
        set(stored_policy.get("deny") or ()) | set(current_policy.get("deny") or ())
    )
    result["capability_policy"] = stored_policy
    result["delivery_effects"] = sorted(
        set(stored.get("delivery_effects") or ()) & set(current.get("capability_policy", {}).get("effects") or ())
    ) if current.get("capability_policy", {}).get("effects") else list(stored.get("delivery_effects") or ())
    result["effect_ceiling"] = list(result["delivery_effects"])
    if current.get("workspace"):
        result["workspace"] = dict(current["workspace"])
    if current.get("resource_budget"):
        result["resource_budget"] = dict(current["resource_budget"])
    result["authority_digest"] = _authority_digest(result)
    return result


def _validate_delivery(delivery: DeliverySpec | None, workspace: Any) -> None:
    if delivery is None:
        return
    channel = str(delivery.channel or "").strip().casefold()
    if channel != "webhook":
        return
    destination = str(delivery.destination or "").strip()
    parsed = urlsplit(destination)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("webhook delivery requires an http(s) destination")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("webhook destination may not contain credentials or fragments")
    policy = getattr(workspace, "network_policy", None)
    _target, error = validate_target(destination, getattr(policy, "value", policy))
    if error:
        raise PermissionError(error)


def _job_delivery_workspace(job: Mapping[str, Any]) -> Any:
    metadata = job.get("metadata")
    authority = metadata.get("_authority_snapshot") if isinstance(metadata, Mapping) else None
    workspace = authority.get("workspace") if isinstance(authority, Mapping) else None
    if not isinstance(workspace, Mapping):
        return None
    return SimpleNamespace(network_policy=workspace.get("network_policy"))


def _control_from_request(request: CapabilityRequest, context: Any) -> ScheduleControl:
    return ScheduleControl(
        origin=request.origin.value,
        task_id=request.task_id,
        session_id=request.session_id,
        principal_id=getattr(context, "principal_id", None),
        project_id=getattr(getattr(context, "workspace", None), "id", None),
        capability_policy=getattr(context, "capability_policy", None),
        resource_budget=getattr(context, "resource_budget", None),
        workspace=getattr(context, "workspace", None),
        narrow_to_caller=bool(request.arguments.get("narrow_authority")),
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
        control: ScheduleControl | None = None,
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
        _validate_delivery(delivery, workspace)
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
        authority["authority_digest"] = _authority_digest(authority)
        control_grant = {
            "creator_task_id": owner_data.get("task_id"),
            "creator_session_id": owner_data.get("session_id"),
            "principal_id": owner_data.get("principal_id"),
            "project_id": owner_data.get("project_id"),
            "authority_digest": authority["authority_digest"],
            "control_scope": "creator_authority_or_operator",
        }
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
            metadata={
                "_owner": owner_data,
                "_authority_snapshot": authority,
                "_control_grant": control_grant,
            },
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
        control: ScheduleControl | None = None,
        authority_mode: str = "preserve",
    ) -> dict | None:
        """Update schedule-owned fields without widening its authority snapshot."""
        job = await self._scheduler._store.get_job_id(job_id)
        if job is None or not _owner_visible(job, owner, control=control):
            return None
        covered = _authority_covers(job, owner, control)
        narrowed = (
            control is not None
            and control.origin == "model"
            and authority_mode == "narrow_to_caller"
            and not covered
        )
        if not covered and not narrowed:
            raise PermissionError("caller authority cannot control this schedule")
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        template = dict(payload.get("template") or {})
        template_metadata = dict(template.get("metadata") or {})
        lineage = dict(template_metadata.get("_schedule_lineage") or {})
        lineage["continuity"] = str(template.get("continuity") or "fresh")
        template_metadata["_schedule_lineage"] = lineage
        template["metadata"] = template_metadata
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
            lineage["continuity"] = continuity
            template_metadata["_schedule_lineage"] = lineage
            template["metadata"] = template_metadata
            if continuity == "session" and not template.get("continuity_session_id"):
                template["continuity_session_id"] = new_id("session")
        if delivery is not _UNSET:
            if delivery is None:
                template["delivery"] = None
            else:
                if not isinstance(delivery, DeliverySpec):
                    raise TypeError("delivery must be a DeliverySpec or None")
                _validate_delivery(
                    delivery,
                    getattr(control, "workspace", None) or _job_delivery_workspace(job),
                )
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
        if narrowed:
            existing_authority = metadata.get("_authority_snapshot")
            if not isinstance(existing_authority, Mapping) or control is None:
                raise PermissionError("schedule authority cannot be narrowed")
            current = _current_authority(control, owner=owner or {})
            metadata["_authority_snapshot"] = _intersect_authority(existing_authority, current)
            metadata["_control_grant"] = {
                **dict(metadata.get("_control_grant") or {}),
                "authority_digest": metadata["_authority_snapshot"]["authority_digest"],
                "control_scope": "narrowed_to_caller",
            }
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
            authority["authority_digest"] = _authority_digest(authority)
            metadata["_authority_snapshot"] = authority
            grant = dict(metadata.get("_control_grant") or {})
            grant["authority_digest"] = authority["authority_digest"]
            metadata["_control_grant"] = grant
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
        self,
        job_id: str,
        *,
        owner: Mapping[str, str | None] | None = None,
        control: ScheduleControl | None = None,
    ) -> str | None:
        job = await self._scheduler._store.get_job_id(job_id)
        if job is None or not _owner_visible(job, owner, control=control):
            return None
        if not _authority_covers(job, owner, control):
            raise PermissionError("caller authority cannot run this schedule")
        return await self._scheduler.run_now(job_id)

    async def list_jobs(
        self,
        *,
        owner: Mapping[str, str | None] | None = None,
        control: ScheduleControl | None = None,
    ) -> list[dict]:
        jobs = await self._scheduler._store.list_jobs(enabled_only=False)
        return [
            self._public_job(job)
            for job in jobs
            if _owner_visible(job, owner, control=control)
        ]

    async def inspect(
        self,
        job_id: str,
        *,
        owner: Mapping[str, str | None] | None = None,
        control: ScheduleControl | None = None,
    ) -> dict | None:
        job = await self._scheduler._store.get_job_id(job_id)
        if job is None or not _owner_visible(job, owner, control=control):
            return None
        return self._public_job(job)

    async def enable(
        self,
        job_id: str,
        *,
        owner: Mapping[str, str | None] | None = None,
        control: ScheduleControl | None = None,
    ) -> bool:
        return await self._set_enabled(job_id, True, owner=owner, control=control)

    async def disable(
        self,
        job_id: str,
        *,
        owner: Mapping[str, str | None] | None = None,
        control: ScheduleControl | None = None,
    ) -> bool:
        return await self._set_enabled(job_id, False, owner=owner, control=control)

    async def delete(
        self,
        job_id: str,
        *,
        owner: Mapping[str, str | None] | None = None,
        control: ScheduleControl | None = None,
    ) -> bool:
        job = await self._scheduler._store.get_job_id(job_id)
        if job is None or not _owner_visible(job, owner, control=control):
            return False
        if not _authority_covers(job, owner, control):
            raise PermissionError("caller authority cannot delete this schedule")
        return await self._scheduler._store.delete_job(job_id)

    async def _set_enabled(
        self,
        job_id: str,
        enabled: bool,
        *,
        owner,
        control: ScheduleControl | None = None,
    ) -> bool:
        job = await self._scheduler._store.get_job_id(job_id)
        if job is None or not _owner_visible(job, owner, control=control):
            return False
        if not _authority_covers(job, owner, control):
            raise PermissionError("caller authority cannot change this schedule")
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
                "narrow_authority": {
                    "type": "boolean",
                    "description": (
                        "For model updates only: intersect future execution authority "
                        "with the caller's current authority."
                    ),
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
        control = _control_from_request(request, context)
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
                    control=control,
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
                jobs = await self._api.list_jobs(owner=owner, control=control)
                return CapabilityResult(
                    call_id,
                    self.descriptor.id,
                    CapabilityResultStatus.OK,
                    output=json.dumps(jobs),
                    metadata={"operation": "list"},
                )
            elif op == "inspect":
                job = await self._api.inspect(
                    args.get("job_id", ""), owner=owner, control=control
                )
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
                ok = await self._api.enable(
                    args.get("job_id", ""), owner=owner, control=control
                )
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
                ok = await self._api.disable(
                    args.get("job_id", ""), owner=owner, control=control
                )
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
                ok = await self._api.delete(
                    args.get("job_id", ""), owner=owner, control=control
                )
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
                    control=control,
                    authority_mode=(
                        "narrow_to_caller" if bool(args.get("narrow_authority")) else "preserve"
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
                task_id = await self._api.run(
                    args.get("job_id", ""), owner=owner, control=control
                )
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
