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
from athena.capabilities.operations import native_descriptor

import json
import os
from typing import Any, Mapping

from athena.protocol.capabilities import (
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
    ResourceClass,
)
from athena.protocol.ids import new_id
from athena.protocol.tasks import (
    DeliverySpec,
)

from athena.scheduler.control import (  # noqa: F401 — compatibility re-export
    ScheduleAPI,
    ScheduleControl,
    control_from_request,
    schedule_effects,
)

_UNSET = object()


class ScheduleCapability:
    """Expose scheduling as a bounded task-lifecycle capability."""

    descriptor = native_descriptor(
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
        effect_resolver=schedule_effects,
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
        control = control_from_request(request, context)
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
                job = await self._api.inspect(args.get("job_id", ""), owner=owner, control=control)
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
                ok = await self._api.enable(args.get("job_id", ""), owner=owner, control=control)
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
                ok = await self._api.disable(args.get("job_id", ""), owner=owner, control=control)
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
                ok = await self._api.delete(args.get("job_id", ""), owner=owner, control=control)
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
                task_id = await self._api.run(args.get("job_id", ""), owner=owner, control=control)
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
