"""Durable maintained-state contracts built on the scheduler."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Mapping

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.ids import new_id
from athena.protocol.tasks import Criterion, VerificationSpec, VerificationType

_logger = logging.getLogger("athena.maintain")


class MaintenanceCapability:
    """Persist an observation/verification/remediation contract.

    The scheduler creates ordinary Athena tasks for each trigger.  The
    contract travels in task metadata, so the normal kernel remains the one
    reasoning authority while the schedule supplies durable re-checks.
    """

    descriptor = CapabilityDescriptor(
        id="maintain",
        description=(
            "Create durable maintained-state contracts: observe a claim, run a "
            "specified verification when its trigger fires, and guide a policy-"
            "bounded remediation. Operations: create/list/inspect/disable/delete."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
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
                    ],
                },
                "contract_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "claim": {"type": "string", "minLength": 1, "maxLength": 4000},
                "observe": {"type": "object", "additionalProperties": True},
                "verify": {"type": "object", "additionalProperties": True},
                "remediation": {"type": "object", "additionalProperties": True},
                "policy": {
                    "type": "string",
                    "enum": [
                        "supervised",
                        "coding",
                        "autonomous",
                    ],
                },
                "trigger": {"type": "object", "maxProperties": 16},
                "fallback_interval_seconds": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 31_536_000,
                },
            },
            "oneOf": [
                {
                    "properties": {"operation": {"const": "create"}},
                    "required": ["claim", "observe", "verify", "trigger"],
                },
                {"properties": {"operation": {"const": "list"}}},
                {
                    "properties": {
                        "operation": {
                            "enum": [
                                "inspect",
                                "enable",
                                "disable",
                                "delete",
                            ]
                        }
                    },
                    "required": ["contract_id"],
                },
            ],
            "additionalProperties": False,
        },
        effects=frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(
        self,
        schedule_api,
        *,
        watch_registry=None,
        workspace=None,
        execution_manager=None,
        fabric=None,
    ) -> None:
        self._schedule = schedule_api
        self._watch_registry = watch_registry
        self._workspace = workspace
        self._execution_manager = execution_manager
        self._fabric = fabric

    async def rehydrate(self) -> int:
        """Restore enabled contract observers after a service restart.

        The scheduler is the durable source of contract state.  Watchers are
        intentionally live process objects, so they are recreated from the
        contract metadata only after the normal capability/runtime surfaces
        have been built.
        """
        if self._watch_registry is None:
            return 0
        owner = {
            "project_id": getattr(self._workspace, "id", None),
        }
        restored = 0
        try:
            contracts = await self._contracts(owner)
        except Exception as exc:
            _logger.warning("maintenance contract rehydration lookup failed: %s", exc)
            return 0
        for contract in contracts:
            if contract.get("status") != "ACTIVE":
                continue
            try:
                if await self._ensure_watch(contract, workspace=self._workspace):
                    restored += 1
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                _logger.warning(
                    "maintenance observer %s could not be restored: %s",
                    contract.get("contract_id"),
                    exc,
                )
        return restored

    async def invoke(self, request: CapabilityRequest, *, context=None, **kw):
        args = dict(request.arguments or {})
        owner = {
            "task_id": request.task_id,
            "session_id": request.session_id,
            "project_id": getattr(getattr(context, "workspace", None), "id", None),
        }
        operation = str(args.get("operation") or "")
        try:
            if operation == "create":
                return await self._create(request, args, owner, context=context)
            contracts = await self._contracts(owner)
            contract_id = str(args.get("contract_id") or "")
            selected = next(
                (item for item in contracts if item["contract_id"] == contract_id),
                None,
            )
            if operation == "list":
                return _result(request, output=json.dumps(contracts))
            if selected is None:
                return _result(request, ok=False, error="maintenance contract not found")
            if operation == "inspect":
                return _result(request, output=json.dumps(selected))
            changed = 0
            if operation == "enable":
                for job in selected["jobs"]:
                    job_id = str(job.get("id") or "")
                    changed += int(await self._schedule.enable(job_id, owner=owner))
                await self._ensure_watch(selected, workspace=self._workspace)
                return _result(
                    request,
                    output=json.dumps(
                        {
                            "contract_id": contract_id,
                            "enabled": True,
                            "jobs_changed": changed,
                        }
                    ),
                )
            for job in selected["jobs"]:
                job_id = str(job.get("id") or "")
                if operation == "delete":
                    ok = await self._schedule.delete(job_id, owner=owner)
                else:
                    ok = await self._schedule.disable(job_id, owner=owner)
                changed += int(ok)
            self._remove_watch(selected)
            if operation == "delete":
                return _result(
                    request,
                    output=json.dumps(
                        {
                            "contract_id": contract_id,
                            "deleted_jobs": changed,
                        }
                    ),
                )
            return _result(
                request,
                output=json.dumps(
                    {
                        "contract_id": contract_id,
                        "enabled": False,
                        "jobs_changed": changed,
                    }
                ),
            )
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return _result(request, ok=False, error=str(exc))

    async def _create(self, request, args, owner, *, context=None):
        trigger = dict(args.get("trigger") or {})
        contract_id = new_id("maintenance")
        observe = dict(args["observe"])
        contract: dict[str, Any] = {
            "contract_id": contract_id,
            "claim": str(args["claim"]),
            "observe": observe,
            "verify": dict(args["verify"]),
            "remediation": dict(args.get("remediation") or {}),
            "policy": str(args.get("policy") or "supervised"),
        }
        workspace = getattr(context, "workspace", None) or self._workspace
        if workspace is not None:
            contract["workspace_id"] = getattr(workspace, "id", None)
            contract["workspace_root"] = os.path.realpath(str(workspace.root))

        acceptance_criteria = _verification_criteria(contract["verify"])
        watch = self._watch_config(observe)
        if watch is not None:
            observe.setdefault("watch_id", new_id("watch"))
            if watch[0] == "process":
                observe["owner_task_id"] = request.task_id
            await self._ensure_watch(
                contract,
                workspace=workspace,
                authority_task_id=request.task_id,
            )
        objective = _objective(contract)
        jobs: list[dict] = []
        metadata = {
            "maintenance_contract": contract,
            "maintenance_role": "primary",
            "project_id": contract.get("workspace_id"),
            # The scheduled task consumes this through the normal dispatch
            # factory; it is a profile selection, never an authority grant.
            "autonomy": contract["policy"],
        }
        workspace_root = getattr(workspace, "root", None)
        watch_id = (contract.get("observe") or {}).get("watch_id")
        trigger_specs: list[tuple[dict, str]] = []
        try:
            if watch_id and str(trigger.get("type") or "") == "event":
                if str(trigger.get("event_name") or "") == "WatchObserved":
                    filters = dict(trigger.get("event_filters") or {})
                    existing = filters.get("watch")
                    if existing is not None and existing != watch_id:
                        raise ValueError("trigger.event_filters.watch conflicts with observer")
                    filters["watch"] = watch_id
                    trigger = {**trigger, "event_filters": filters}
                trigger_specs.append((trigger, "primary"))
            else:
                trigger_specs.append((trigger, "primary"))
                if watch_id:
                    trigger_specs.append(
                        (
                            {
                                "type": "event",
                                "event_name": "WatchObserved",
                                "event_filters": {"watch": watch_id},
                            },
                            "observer",
                        )
                    )
            fallback = args.get("fallback_interval_seconds")
            if fallback is not None:
                interval_trigger = {
                    "type": "interval",
                    "interval_seconds": float(fallback),
                }
                trigger_specs.append((interval_trigger, "fallback"))
            for trigger_spec, role in trigger_specs:
                jobs.append(
                    await self._schedule.create(
                        name=f"maintain {contract_id}" + (f" {role}" if role != "primary" else ""),
                        objective=objective,
                        trigger=trigger_spec,
                        session_id=request.session_id,
                        workspace_root=workspace_root,
                        workspace=workspace,
                        acceptance_criteria=acceptance_criteria,
                        owner=owner,
                        metadata={**metadata, "maintenance_role": role},
                    )
                )
        except Exception:
            for job in jobs:
                await self._schedule.delete(
                    str(job.get("job_id") or job.get("id") or ""), owner=owner
                )
            self._remove_watch(contract)
            raise
        return _result(
            request,
            output=json.dumps(
                {
                    **contract,
                    "jobs": jobs,
                    "status": "ACTIVE",
                }
            ),
        )

    async def _contracts(self, owner) -> list[dict]:
        jobs = await self._schedule.list_jobs(owner=owner)
        grouped: dict[str, dict] = {}
        for job in jobs:
            metadata = job.get("metadata") or {}
            template = job.get("template") or {}
            contract = metadata.get("maintenance_contract") or (template.get("metadata") or {}).get(
                "maintenance_contract"
            )
            if not isinstance(contract, Mapping):
                continue
            contract_id = str(contract.get("contract_id") or "")
            if not contract_id:
                continue
            record = grouped.setdefault(
                contract_id,
                {
                    **dict(contract),
                    "status": "ACTIVE",
                    "jobs": [],
                },
            )
            record["jobs"].append(job)
        for record in grouped.values():
            record["status"] = (
                "ACTIVE"
                if record["jobs"] and all(job.get("enabled", True) for job in record["jobs"])
                else "DISABLED"
            )
        return sorted(grouped.values(), key=lambda item: item["contract_id"])

    @staticmethod
    def _watch_config(observe: Mapping[str, Any]) -> tuple[str, dict] | None:
        kind = str(observe.get("kind") or observe.get("type") or "").lower()
        if kind in {"file", "files", "filesystem"}:
            return "file", dict(observe)
        if kind in {"process", "proc"}:
            return "process", dict(observe)
        return None

    async def _ensure_watch(
        self,
        contract: Mapping[str, Any],
        *,
        workspace=None,
        authority_task_id: str | None = None,
    ) -> bool:
        if self._watch_registry is None:
            return False
        observe = contract.get("observe")
        if not isinstance(observe, Mapping):
            return False
        config = self._watch_config(observe)
        if config is None:
            return False
        kind, values = config
        watch_id = str(values.get("watch_id") or "")
        if not watch_id:
            raise ValueError("durable observer requires watch_id")
        workspace = workspace or self._workspace
        observer_id = str(values.get("observer_id") or "") or None
        if observer_id and self._fabric is not None:
            provenance = self._fabric.provenance(observer_id)
            if provenance and provenance.get("task_scope"):
                raise ValueError(
                    "durable maintenance observers must be promoted to project or user scope"
                )
            if provenance:
                # Resolve through the effective durable overlay now and on
                # restart. A task-local generated observer must never remain
                # as a dangling watcher after its creating task finishes.
                self._fabric.executor_for(
                    observer_id,
                    project_id=getattr(workspace, "id", None),
                    user_id="athena",
                )
        if kind == "file":
            if workspace is None:
                raise ValueError("file maintenance observer requires workspace")
            base = os.path.realpath(str(workspace.root))
            requested = str(values.get("path") or ".")
            path = os.path.realpath(
                requested if os.path.isabs(requested) else os.path.join(base, requested)
            )
            if path != base and not path.startswith(base + os.sep):
                raise ValueError("maintenance observer path outside workspace")
            self._watch_registry.add_file(
                root=path,
                pattern=str(values.get("pattern") or "*"),
                task_id=None,
                watch_id=watch_id,
                max_files=int(values.get("max_files") or 10_000),
                max_bytes_per_poll=int(values.get("max_bytes_per_poll") or 10 * 1024 * 1024),
                ignore_patterns=tuple(str(item) for item in (values.get("ignore") or ())),
                interval=float(values.get("interval") or 0.0),
                debounce=float(values.get("debounce") or 0.0),
                workspace=workspace,
                observer_id=observer_id,
            )
            return True

        pid = int(values.get("pid") or 0)
        if self._execution_manager is None:
            raise ValueError("process maintenance observer requires execution manager")
        from athena.capabilities.watch import _process_identity

        start_identity = str(values.get("start_identity") or _process_identity(pid) or "")
        owner_task_id = str(values.get("owner_task_id") or authority_task_id or "")
        if not self._execution_manager.owns_process(
            pid=pid, task_id=owner_task_id, start_identity=start_identity
        ):
            raise ValueError("process maintenance observer is not Athena-owned")
        values["start_identity"] = start_identity
        if isinstance(contract.get("observe"), dict):
            contract["observe"]["start_identity"] = start_identity
        self._watch_registry.add_process(
            pid=pid,
            start_identity=start_identity,
            task_id=None,
            watch_id=watch_id,
            workspace=workspace,
            observer_id=observer_id,
        )
        return True

    def _remove_watch(self, contract: Mapping[str, Any]) -> None:
        if self._watch_registry is None:
            return
        observe = contract.get("observe")
        if isinstance(observe, Mapping):
            watch_id = str(observe.get("watch_id") or "")
            if watch_id:
                self._watch_registry.remove(watch_id)


def _objective(contract: Mapping[str, Any]) -> str:
    return (
        "Maintenance run. Claim: {claim}\n"
        "Observe: {observe}\n"
        "Verify: {verify}\n"
        "Remediation (only under policy): {remediation}\n"
        "Policy: {policy}\n"
        "Record current truth and leave the claim stale/unknown when verification "
        "cannot be completed."
    ).format(
        claim=contract["claim"],
        observe=json.dumps(contract["observe"], sort_keys=True),
        verify=json.dumps(contract["verify"], sort_keys=True),
        remediation=json.dumps(contract["remediation"], sort_keys=True),
        policy=contract["policy"],
    )


def _verification_criteria(verify: Mapping[str, Any]) -> tuple[Criterion, ...]:
    """Translate a maintenance verifier into the normal task verifier path."""
    values = dict(verify or {})
    nested = values.get("arguments")
    arguments = dict(nested) if isinstance(nested, Mapping) else {}
    command = values.get("command") or arguments.get("command")
    path = values.get("path") or arguments.get("path")
    predicate = values.get("predicate") or arguments.get("predicate")
    capability = values.get("capability") or values.get("capability_id")
    raw_type = str(values.get("type") or "").lower()

    if command:
        verification = VerificationSpec(
            type=VerificationType.COMMAND,
            command=str(command),
        )
    elif path:
        verification = VerificationSpec(
            type=VerificationType.FILE,
            path=str(path),
            predicate=str(predicate) if predicate is not None else None,
        )
    elif capability:
        verification = VerificationSpec(
            type=VerificationType.CAPABILITY_CHECK,
            capability=str(capability),
        )
    elif raw_type == VerificationType.MANUAL.value:
        verification = VerificationSpec(type=VerificationType.MANUAL)
    else:
        raise ValueError(
            "maintenance verify must specify command, path, capability_id, or type=manual"
        )
    return (
        Criterion(
            id="maintenance_verification",
            description=json.dumps(values, sort_keys=True),
            verification=verification,
            required=True,
        ),
    )


def _result(request, *, ok: bool = True, output: str = "", error: str | None = None):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
    )


__all__ = ["MaintenanceCapability"]
