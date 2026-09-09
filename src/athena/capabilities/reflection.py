"""Capability reflection: query the effective affordance surface."""

from __future__ import annotations

from collections.abc import Mapping
import inspect
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from athena.execution.environment import ProjectEnvironmentFingerprint
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.errors import CapabilityUnavailable
from athena.protocol.tasks import WorkspaceSpec


class CapabilityReflection:
    descriptor = CapabilityDescriptor(
        id="capabilities",
        description=(
            "Reflect on the effective capability fabric: search and describe "
            "available capabilities, inspect dependencies and provenance, "
            "review lifecycle history, and list machinery created this task."
        ),
        tags=frozenset(
            {"capability", "capabilities", "tool", "tools", "affordance", "discover", "discovery"}
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "search",
                        "describe",
                        "dependencies",
                        "provenance",
                        "history",
                        "created_this_task",
                        "workflows",
                        "skills",
                        "runtimes",
                        "permissions",
                        "devices",
                        "availability",
                    ],
                },
                "query": {"type": "string"},
                "capability_id": {"type": "string"},
                "capability_arguments": {"type": "object"},
                "workflow_id": {"type": "string"},
                "skill_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
        effects=frozenset({EffectClass.READ_LOCAL}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(
        self,
        fabric,
        *,
        workflow_store=None,
        skills_store=None,
        execution_manager=None,
        device_provider=None,
        policy_engine=None,
        approval_store=None,
        health_provider=None,
        runtime_health_provider=None,
        model_provider=None,
        mcp_status_provider=None,
        delegate_provider=None,
    ) -> None:
        self._fabric = fabric
        self._workflows = workflow_store
        self._skills = skills_store
        self._execution = execution_manager
        self._devices = device_provider
        self._policy = policy_engine
        self._approvals = approval_store
        self._health = health_provider
        self._runtime_health = runtime_health_provider
        self._models = model_provider
        self._mcp_status = mcp_status_provider
        self._delegates = delegate_provider

    async def invoke(self, request: CapabilityRequest, **kw) -> CapabilityResult:
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")
        task_id = request.task_id
        context = kw.get("context")
        raw_workspace = getattr(context, "workspace", None)
        workspace: WorkspaceSpec | None = (
            raw_workspace if isinstance(raw_workspace, WorkspaceSpec) else None
        )
        project_id = getattr(workspace, "id", None)
        user_id = getattr(context, "principal_id", None)
        try:
            if operation == "search":
                # Method-local import: fabric imports the capability registry,
                # which re-enters this package at module load time.
                from athena.affordances.fabric import EXPLICIT_REFLECTION_SEARCH

                value = self._fabric.search(
                    str(args.get("query") or ""),
                    task_id=task_id,
                    project_id=project_id,
                    user_id=user_id,
                    workspace=workspace,
                    # The final limit belongs to the unified search, not to
                    # one affordance family. Keep enough candidates from the
                    # capability surface for workflows and skills to compete.
                    limit=10_000,
                    # An operator/model-initiated reflection search is an
                    # explicit query: take it literally instead of applying
                    # the conservative automatic-disclosure filter.
                    mode=EXPLICIT_REFLECTION_SEARCH,
                )
                value = await self._search_other_affordances(
                    value,
                    str(args.get("query") or ""),
                    task_id=task_id,
                    project_id=project_id,
                    user_id=user_id,
                    limit=int(args.get("limit") or 20),
                )
            elif operation == "describe":
                if args.get("workflow_id"):
                    value = await self._describe_workflow(
                        str(args["workflow_id"]),
                        task_id=task_id,
                        project_id=project_id,
                        user_id=user_id,
                    )
                elif args.get("skill_id"):
                    value = await self._describe_skill(str(args["skill_id"]))
                else:
                    value = self._fabric.describe(
                        str(args.get("capability_id") or ""),
                        task_id=task_id,
                        project_id=project_id,
                        user_id=user_id,
                    )
            elif operation == "dependencies":
                value = self._fabric.dependencies(str(args.get("capability_id") or ""))
            elif operation == "provenance":
                value = self._fabric.provenance(str(args.get("capability_id") or ""))
            elif operation == "history":
                value = self._fabric.history(str(args.get("capability_id") or ""))
            elif operation == "created_this_task":
                value = self._fabric.created_this_task(task_id)
            elif operation == "workflows":
                value = await self._list_workflows(
                    task_id=task_id,
                    project_id=project_id,
                    user_id=user_id,
                    query=str(args.get("query") or ""),
                    limit=int(args.get("limit") or 20),
                )
            elif operation == "skills":
                value = await self._list_skills(
                    query=str(args.get("query") or ""),
                    limit=int(args.get("limit") or 20),
                )
            elif operation == "runtimes":
                value = self._list_runtimes()
            elif operation == "permissions":
                value = await self._list_permissions(
                    capability_id=str(args.get("capability_id") or ""),
                    task_id=task_id,
                    project_id=project_id,
                    user_id=user_id,
                    context=context,
                )
            elif operation == "devices":
                value = self._list_devices()
            elif operation == "availability":
                capability_id = str(args.get("capability_id") or "")
                value = (
                    self._explain_availability(
                        capability_id,
                        dict(args.get("capability_arguments") or {}),
                        task_id=task_id,
                        project_id=project_id,
                        user_id=user_id,
                        context=context,
                    )
                    if capability_id
                    else await self._environment_passport(
                        task_id=task_id,
                        project_id=project_id,
                        user_id=user_id,
                        context=context,
                    )
                )
            else:
                return _result(request, ok=False, error=f"unknown operation: {operation}")
            import json

            return _result(request, output=json.dumps(value, default=str))
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return _result(request, ok=False, error=str(exc))

    async def _search_other_affordances(
        self,
        values: list[dict],
        query: str,
        *,
        task_id: str | None,
        project_id: str | None,
        user_id: str | None,
        limit: int,
    ) -> list[dict]:
        """Rank capabilities, workflows, and skills as one surface.

        Search is intentionally lexical and deterministic. Scope and proven
        observations provide bounded tie-breakers; this is discovery, not an
        authority grant or a claim that an affordance is currently runnable.
        """
        ranked: list[tuple[float, dict]] = []
        for item in values:
            score = float(item.get("score", 0.0))
            ranked.append((score, {"kind": "capability", **item}))
        terms = {term.casefold() for term in re.findall(r"[a-zA-Z0-9_.-]+", query)}

        def lexical_score(*parts: str) -> float:
            haystack = " ".join(parts).casefold()
            if not terms:
                return 0.0
            matched = sum(1 for term in terms if term in haystack)
            if not matched:
                return 0.0
            return float(matched + (1 if all(term in haystack for term in terms) else 0))

        if self._workflows is not None:
            workflows = await self._workflows.list(
                task_id=task_id,
                project_id=project_id,
                user_id=user_id,
            )
            for workflow in workflows:
                score = lexical_score(
                    workflow.id,
                    workflow.name,
                    workflow.description,
                )
                if not terms or score:
                    provenance = dict(workflow.provenance or {})
                    observations = min(
                        1.0, int(provenance.get("successful_observations") or 0) / 10.0
                    )
                    ranked.append(
                        (
                            score + observations,
                            {
                                "kind": "workflow",
                                "id": workflow.id,
                                "description": workflow.description,
                                "origin": workflow.scope.value,
                                "effects": [],
                                "scope": workflow.scope.value,
                                "score": score + observations,
                                "steps": len(workflow.steps),
                            },
                        )
                    )
        if self._skills is not None:
            for skill in await self._skills.search(query=query, limit=limit):
                description = skill.describe()
                score = lexical_score(skill.id, skill.name, description)
                if not terms or score:
                    scope_bonus = 0.5 if skill.scope != "global" else 0.0
                    ranked.append(
                        (
                            score + scope_bonus,
                            {
                                "kind": "skill",
                                "id": skill.id,
                                "description": description,
                                "origin": skill.scope,
                                "effects": [],
                                "scope": skill.scope,
                                "score": score + scope_bonus,
                                "version": skill.version,
                            },
                        )
                    )
        ranked.sort(key=lambda item: (-item[0], item[1]["kind"], item[1]["id"]))
        return [item for _, item in ranked[: max(limit, 0)]]

    async def _list_workflows(
        self, *, task_id, project_id, user_id, query: str, limit: int
    ) -> list[dict]:
        if self._workflows is None:
            return []
        workflows = await self._workflows.list(
            task_id=task_id,
            project_id=project_id,
            user_id=user_id,
        )
        terms = {term.casefold() for term in query.split() if term.strip()}
        return [
            {
                "kind": "workflow",
                "id": workflow.id,
                "name": workflow.name,
                "description": workflow.description,
                "scope": workflow.scope.value,
                "steps": len(workflow.steps),
            }
            for workflow in workflows
            if not terms
            or all(
                term in f"{workflow.id} {workflow.name} {workflow.description}".casefold()
                for term in terms
            )
        ][: max(limit, 0)]

    async def _list_skills(self, *, query: str, limit: int) -> list[dict]:
        if self._skills is None:
            return []
        skills = await self._skills.search(query=query, limit=limit)
        return [
            {
                "kind": "skill",
                "id": skill.id,
                "name": skill.name,
                "description": skill.description,
                "scope": skill.scope,
                "version": skill.version,
            }
            for skill in skills
        ][: max(limit, 0)]

    def _list_runtimes(self) -> list[dict]:
        if self._execution is None:
            return []
        status = getattr(self._execution, "runtime_status", None)
        if callable(status):
            return [self._availability_record(item, kind="runtime") for item in status()]
        names = self._execution.available_runtimes()
        return [
            self._availability_record(
                {"kind": "runtime", "id": name, "available": True},
                kind="runtime",
            )
            for name in names
        ]

    @staticmethod
    def _availability_record(record: Mapping[str, Any], *, kind: str) -> dict[str, Any]:
        """Add an actionable reason/remediation without hiding raw health facts."""
        result = dict(record)
        result.setdefault("kind", kind)
        identifier = str(result.get("id") or result.get("name") or kind)
        raw_status = str(result.get("status") or "")
        available = result.get("available")
        normalized_status = raw_status.casefold()
        unavailable = available is False or normalized_status in {
            "unavailable",
            "missing",
            "unsupported",
            "error",
        }
        known_available = available is True or normalized_status in {
            "available",
            "ok",
            "ready",
            "healthy",
            "connected",
            "active",
            "configured",
        }
        # Retain the provider's status for compatibility. availability is
        # the canonical two-valued field used by the passport.
        result["availability"] = (
            "unavailable" if unavailable or not known_available else "available"
        )
        result.setdefault("status", "unavailable" if unavailable else "available")
        result.setdefault(
            "reason",
            f"{kind} {identifier!r} is unavailable"
            if result["availability"] == "unavailable"
            else None,
        )
        result.setdefault(
            "remediation",
            f"configure or install {kind} {identifier!r}"
            if result["availability"] == "unavailable"
            else None,
        )
        return result

    async def _list_permissions(
        self,
        *,
        capability_id: str,
        task_id: str | None,
        project_id: str | None,
        user_id: str | None,
        context=None,
    ) -> list[dict]:
        descriptors = self._fabric.list_descriptors(
            task_id=task_id,
            project_id=project_id,
            user_id=user_id,
        )
        if capability_id:
            descriptors = [item for item in descriptors if item.id == capability_id]

        task_policy = getattr(context, "capability_policy", None)
        task_policy_record = None
        if task_policy is not None:
            task_policy_record = {
                "allow": list(task_policy.allow),
                "ask": list(task_policy.ask),
                "deny": list(task_policy.deny),
                "effects": sorted(
                    getattr(effect, "value", str(effect)) for effect in task_policy.effects
                ),
            }

        pending: list[dict] = []
        if self._approvals is not None and task_id is not None:
            for record in await self._approvals.list_pending(task_id):
                # Approval arguments are intentionally omitted: reflection is
                # model-visible and arguments may contain credentials or data.
                pending.append(
                    {
                        "approval_id": record.get("id"),
                        "capability_id": record.get("capability_id"),
                        "status": record.get("status"),
                        "created_at": record.get("created_at"),
                    }
                )

        grants: list[dict] = []
        manager = getattr(self._policy, "approvals", None)
        if manager is not None:
            for grant in manager.list_active():
                if grant.task_id not in (None, task_id):
                    continue
                grants.append(
                    {
                        "approval_id": grant.id,
                        "capability_id": grant.capability,
                        "effect": getattr(grant.effect, "value", grant.effect),
                        "scope": getattr(grant.scope, "value", grant.scope),
                        "resource_pattern": grant.resource_pattern,
                        "task_id": grant.task_id,
                        "session_id": grant.session_id,
                        "expires_at": (grant.expires_at.isoformat() if grant.expires_at else None),
                    }
                )

        workspace = getattr(context, "workspace", None)
        raw_profile = getattr(self._policy, "profile", None)
        profile = getattr(raw_profile, "value", raw_profile)
        permissions = [
            {
                "kind": "permission",
                "capability_id": descriptor.id,
                "declared_effects": sorted(effect.value for effect in descriptor.effects),
                "availability": descriptor.availability.value,
                "task_allowed": not (
                    task_policy is not None
                    and (
                        descriptor.id in task_policy.deny
                        or (bool(task_policy.allow) and descriptor.id not in task_policy.allow)
                    )
                ),
                "task_requires_approval": bool(
                    task_policy is not None and descriptor.id in task_policy.ask
                ),
                "task_effect_ceiling": task_policy_record["effects"]
                if task_policy_record is not None
                else [],
            }
            for descriptor in descriptors
        ]
        return [
            {
                "kind": "policy_context",
                "profile": profile,
                "workspace_id": getattr(workspace, "id", None),
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
                "task_policy": task_policy_record,
                "pending_approvals": pending,
                "active_grants": grants,
            },
            *permissions,
        ]

    def _list_devices(self) -> list[dict]:
        """Return registered device adapters without inventing support."""
        if self._devices is None:
            return [
                {
                    "kind": "device_provider",
                    "status": "unsupported",
                    "reason": "no device provider is configured",
                }
            ]
        value = self._devices() if callable(self._devices) else self._devices
        devices = list(value or ())
        return devices or [
            {
                "kind": "device_provider",
                "status": "unsupported",
                "reason": "configured device provider returned no adapters",
            }
        ]

    def _explain_availability(
        self,
        capability_id: str,
        capability_arguments: dict,
        *,
        task_id: str | None,
        project_id: str | None,
        user_id: str | None,
        context=None,
    ) -> dict:
        """Compute whether a capability can run in this task context.

        Reflection is advisory and never grants authority. It combines the
        effective fabric, runtime inventory, workspace network boundary, and
        task policy into a bounded explanation so the kernel can resolve a
        missing precondition deliberately instead of trial-and-error calls.
        """
        checks: list[dict] = []
        preconditions: list[str] = []
        descriptor = None
        provenance = self._fabric.provenance(capability_id)
        try:
            descriptor = self._fabric.executor_for(
                capability_id,
                task_id=task_id,
                project_id=project_id,
                user_id=user_id,
            ).descriptor
            checks.append({"kind": "capability", "status": "available"})
        except CapabilityUnavailable as exc:
            lifecycle = str((provenance or {}).get("lifecycle_state") or "")
            status = (
                "stale"
                if lifecycle
                in {
                    "STALE",
                    "REVALIDATION_REQUIRED",
                }
                else "unavailable"
            )
            checks.append(
                {
                    "kind": "capability",
                    "status": status,
                    "detail": str(exc),
                    **({"lifecycle_state": lifecycle} if lifecycle else {}),
                }
            )

        if descriptor is None:
            return {
                "kind": "environment_passport",
                "capability_id": capability_id,
                "status": "BLOCKED",
                "checks": checks,
                "preconditions": preconditions,
                "next_steps": ["inspect the capability surface or build a replacement"],
            }

        workspace = getattr(context, "workspace", None)
        dependency_available, environment_compatible = self._fabric.prerequisite_status(
            capability_id,
            task_id=task_id,
            project_id=project_id,
            user_id=user_id,
            workspace=workspace,
        )
        if not dependency_available:
            checks.append(
                {
                    "kind": "prerequisites",
                    "status": "missing",
                    "detail": "required capabilities or dependencies are unavailable",
                }
            )
            preconditions.append("required capability/dependency prerequisites are unavailable")
        elif not environment_compatible:
            checks.append(
                {
                    "kind": "environment",
                    "status": "incompatible",
                    "detail": "the current environment does not match the capability proof",
                }
            )
            preconditions.append("the current environment does not match capability proof")

        if self._health is not None:
            health = self._health.get(capability_id)
            health_status = str(health.get("status") or "closed")
            checks.append(
                {
                    "kind": "health",
                    "status": "open"
                    if health_status == "open"
                    else ("probing" if health_status == "half_open" else "healthy"),
                    "consecutive_failures": health.get("consecutive_failures", 0),
                    "retry_after_seconds": health.get("retry_after_seconds", 0.0),
                }
            )
            if health_status == "open":
                preconditions.append("capability circuit is open; wait for its cooldown")

        try:
            effects = descriptor.resolve_effects(capability_arguments)
        except (TypeError, ValueError):
            effects = None
        effective_effects = effects or descriptor.effects
        effect_values = {getattr(effect, "value", str(effect)) for effect in effective_effects}

        runtime_name = (
            capability_arguments.get("runtime")
            or capability_arguments.get("language")
            or (provenance or {}).get("runtime")
        )
        if runtime_name and self._execution is not None:
            runtime_available = self._execution.has_runtime(str(runtime_name))
            checks.append(
                {
                    "kind": "runtime",
                    "id": str(runtime_name),
                    "status": "available" if runtime_available else "missing",
                }
            )
            if not runtime_available:
                preconditions.append(f"runtime {runtime_name!r} is unavailable")

        execution_backend = getattr(workspace, "execution_backend", None)
        if execution_backend and self._execution is not None:
            backend_status = getattr(self._execution, "backend_status", None)
            statuses = list(backend_status()) if callable(backend_status) else []
            selected = next(
                (item for item in statuses if item.get("id") == execution_backend),
                None,
            )
            if selected is not None:
                backend_available = bool(selected.get("available", False))
                checks.append(
                    {
                        "kind": "execution_backend",
                        "id": execution_backend,
                        "status": "available" if backend_available else "missing",
                        "healthy": bool(selected.get("healthy", backend_available)),
                    }
                )
                if not backend_available:
                    preconditions.append(f"execution backend {execution_backend!r} is unavailable")
        network_policy = getattr(
            getattr(workspace, "network_policy", None),
            "value",
            getattr(workspace, "network_policy", None),
        )
        needs_network = bool({"NETWORK_READ", "NETWORK_WRITE"} & effect_values)
        if needs_network and network_policy == "deny":
            checks.append(
                {
                    "kind": "network",
                    "status": "blocked",
                    "detail": "workspace network policy is deny",
                }
            )
            preconditions.append("workspace network policy must allow this operation")
        elif needs_network:
            browser_restricted_unavailable = (
                capability_id == "browser" and network_policy == "restricted"
            )
            checks.append(
                {
                    "kind": "network",
                    "status": (
                        "unavailable"
                        if browser_restricted_unavailable
                        else ("restricted" if network_policy == "restricted" else "available")
                    ),
                    "policy": network_policy or "unknown",
                    **(
                        {
                            "detail": (
                                "browser restricted networking requires an Athena-controlled "
                                "DNS-pinned proxy"
                            )
                        }
                        if browser_restricted_unavailable
                        else {}
                    ),
                }
            )
            if browser_restricted_unavailable:
                preconditions.append(
                    "browser driver must advertise Athena-controlled DNS-pinned proxy enforcement"
                )

        task_policy = getattr(context, "capability_policy", None)
        policy_status = "allowed"
        if task_policy is not None:
            if capability_id in task_policy.deny or (
                task_policy.allow and capability_id not in task_policy.allow
            ):
                policy_status = "denied"
                preconditions.append("task capability policy denies this capability")
            elif capability_id in task_policy.ask:
                policy_status = "approval_required"
                preconditions.append("operator approval is required")
            ceiling = {
                getattr(effect, "value", str(effect)) for effect in (task_policy.effects or ())
            }
            if ceiling and not effect_values.issubset(ceiling):
                policy_status = "denied"
                preconditions.append("capability effects exceed the task ceiling")
        checks.append(
            {
                "kind": "policy",
                "status": policy_status,
                "effects": sorted(effect_values),
            }
        )

        blocked = any(
            item["status"] in {"missing", "blocked", "denied", "stale", "open"} for item in checks
        )
        approval = any(item["status"] == "approval_required" for item in checks)
        status = "BLOCKED" if blocked else "REQUIRES_APPROVAL" if approval else "AVAILABLE"
        return {
            "kind": "environment_passport",
            "capability_id": capability_id,
            "status": status,
            "checks": checks,
            "preconditions": preconditions,
            "next_steps": (
                ["resolve the listed preconditions before invoking"] if preconditions else []
            ),
        }

    async def _environment_passport(
        self,
        *,
        task_id: str | None,
        project_id: str | None,
        user_id: str | None,
        context=None,
    ) -> dict:
        """Summarize the effective machine/task surface in one graph."""
        subsystem_health: dict[str, Any] = {}
        if self._runtime_health is not None:
            try:
                raw_health = self._runtime_health()
                if inspect.isawaitable(raw_health):
                    raw_health = await raw_health
                if isinstance(raw_health, Mapping):
                    subsystem_health = {
                        str(name): dict(value) if isinstance(value, Mapping) else value
                        for name, value in raw_health.items()
                    }
            except Exception as exc:  # noqa: BLE001 - reflection is advisory
                subsystem_health = {
                    "service_runtime": {
                        "health": "unavailable",
                        "error": str(exc),
                    }
                }
        capabilities = []
        for descriptor in self._fabric.list_descriptors(
            task_id=task_id,
            project_id=project_id,
            user_id=user_id,
        ):
            item = self._explain_availability(
                descriptor.id,
                {},
                task_id=task_id,
                project_id=project_id,
                user_id=user_id,
                context=context,
            )
            capabilities.append(
                {
                    "id": descriptor.id,
                    "status": item["status"],
                    "availability": (
                        "available" if item["status"] == "AVAILABLE" else "unavailable"
                    ),
                    "preconditions": item["preconditions"],
                    "checks": item["checks"],
                    "reason": item["preconditions"][0] if item["preconditions"] else None,
                    "remediation": (
                        "resolve the listed preconditions before invoking"
                        if item["preconditions"]
                        else None
                    ),
                }
            )
        raw_workspace = getattr(context, "workspace", None)
        workspace: WorkspaceSpec | None = (
            raw_workspace if isinstance(raw_workspace, WorkspaceSpec) else None
        )
        environment = None
        backends = []
        if self._execution is not None:
            status = getattr(self._execution, "backend_status", None)
            if callable(status):
                backends = list(status())
        backend_records = [
            self._availability_record(item, kind="execution_backend") for item in backends
        ]
        if not backend_records:
            backend_records = [
                {
                    "kind": "execution_backend_provider",
                    "status": "unavailable",
                    "availability": "unavailable",
                    "reason": "no execution backend inventory is configured",
                    "remediation": "start the execution manager and register a backend",
                }
            ]
        runtime_records = self._list_runtimes()
        if not runtime_records:
            runtime_records = [
                {
                    "kind": "runtime_provider",
                    "status": "unavailable",
                    "availability": "unavailable",
                    "reason": "no runtime inventory is configured",
                    "remediation": "start the execution manager and register a runtime",
                }
            ]
        environment_extras = {"backends": backends} if backends else None
        if workspace is not None:
            environment = ProjectEnvironmentFingerprint().describe(
                workspace,
                extras=environment_extras,
            )
        environment_fingerprint = (
            ProjectEnvironmentFingerprint().fingerprint(
                workspace,
                extras=environment_extras,
            )
            if workspace is not None
            else None
        )
        model_registry = self._models() if callable(self._models) else self._models
        configured_models: list[dict] = []
        model_provider_names: list[str] = []
        model_reason = "no model provider is configured"
        provider_readiness: dict[str, Any] = {}
        if model_registry is not None:
            try:
                model_provider_names = list(model_registry.names())
                readiness_probe = getattr(model_registry, "readiness", None)
                if callable(readiness_probe):
                    raw_readiness = readiness_probe()
                    if isinstance(raw_readiness, Mapping):
                        provider_readiness = dict(raw_readiness)
                models = await model_registry.list_models()
                provider_states = {
                    str(name): str(record.get("state") or "unverified")
                    for name, record in dict(provider_readiness.get("providers") or {}).items()
                    if isinstance(record, Mapping)
                }
                configured_models = [
                    {
                        "id": getattr(model, "id", None),
                        "provider": getattr(model, "provider", None),
                        "context_window": getattr(model, "context_window", None),
                        "status": (
                            "available"
                            if provider_states.get(str(getattr(model, "provider", "")), "ready")
                            == "ready"
                            else "unavailable"
                        ),
                    }
                    for model in models
                ]
                if not configured_models:
                    model_reason = "configured providers expose no models"
                elif not any(item["status"] == "available" for item in configured_models):
                    model_reason = "configured providers are not ready"
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                model_reason = f"model inventory unavailable: {exc}"

        model_available = any(item["status"] == "available" for item in configured_models)

        toolchain_names = ("python", "uv", "ruff", "mypy", "pytest", "cargo", "rustc", "node")
        toolchains = [
            {
                "name": name,
                "executable": shutil.which(name),
                "status": "available" if shutil.which(name) else "unavailable",
                "reason": None if shutil.which(name) else f"{name} is not installed or not on PATH",
                "remediation": None if shutil.which(name) else f"install or configure {name}",
            }
            for name in toolchain_names
        ]
        workspace_root = Path(workspace.root).resolve() if workspace is not None else None
        sandbox_available = os.name == "posix" and shutil.which("bwrap") is not None
        sandbox_status = (
            "available"
            if sandbox_available
            else ("unsupported" if os.name != "posix" else "unavailable")
        )
        filesystem = {
            "workspace_root": str(workspace_root) if workspace_root else None,
            "workspace_exists": bool(workspace_root and workspace_root.is_dir()),
            "workspace_writable": bool(workspace_root and os.access(workspace_root, os.W_OK)),
            "free_bytes": (
                shutil.disk_usage(workspace_root).free
                if workspace_root and workspace_root.exists()
                else None
            ),
            "sandbox_backend": "bubblewrap" if sandbox_available else None,
            "sandbox_status": sandbox_status,
            "sandbox_remediation": None
            if sandbox_available
            else (
                "restricted execution is supported on POSIX hosts only"
                if os.name != "posix"
                else "install bubblewrap before invoking restricted execution"
            ),
        }
        filesystem_available = bool(
            filesystem["workspace_exists"]
            and filesystem["workspace_writable"]
            and filesystem["sandbox_status"] == "available"
        )
        filesystem["status"] = "available" if filesystem_available else "unavailable"
        filesystem["availability"] = filesystem["status"]
        filesystem["reason"] = (
            None
            if filesystem_available
            else (
                "workspace context is missing or not writable"
                if not filesystem["workspace_exists"] or not filesystem["workspace_writable"]
                else "restricted sandbox backend is unavailable"
            )
        )
        filesystem["remediation"] = (
            None
            if filesystem_available
            else (
                "supply a writable workspace root"
                if not filesystem["workspace_exists"] or not filesystem["workspace_writable"]
                else filesystem["sandbox_remediation"]
            )
        )
        network_policy = (
            getattr(getattr(workspace, "network_policy", None), "value", None)
            if workspace is not None
            else None
        )
        configured_connectivity = "configured" if network_policy is not None else "unknown"
        physical_connectivity = os.environ.get("ATHENA_NETWORK_CONNECTIVITY", "").strip().lower()
        if physical_connectivity not in {"available", "unavailable"}:
            physical_connectivity = "unknown"
        if workspace is None:
            network_state = "unknown"
            network_reason = "no workspace context supplied; physical connectivity is unverified"
        elif network_policy == "deny":
            network_state = "blocked"
            network_reason = "workspace network policy is deny"
        elif network_policy == "restricted":
            network_state = "restricted"
            network_reason = "workspace network policy restricts network access"
        elif physical_connectivity == "available":
            network_state = "available"
            network_reason = None
        else:
            network_state = "unknown"
            network_reason = (
                "workspace policy permits network, but physical connectivity is unverified"
            )
        network = {
            "policy": network_policy,
            "status": network_state,
            "configured_connectivity": configured_connectivity,
            "physical_connectivity": physical_connectivity,
            "availability": "available" if network_state == "available" else "unavailable",
            "reason": network_reason,
            "remediation": (
                None
                if network_state == "available"
                else (
                    "supply a workspace context before requesting networked work"
                    if workspace is None
                    else "verify connectivity or set ATHENA_NETWORK_CONNECTIVITY=available"
                    if network_state == "unknown"
                    else "request an explicit network policy that permits this operation"
                )
            ),
        }
        mcp_status = self._mcp_status() if callable(self._mcp_status) else self._mcp_status
        mcp = []
        for name, value in sorted(dict(mcp_status or {}).items()):
            if isinstance(value, Mapping):
                state = str(value.get("state") or "unknown")
                record = dict(value)
                record.setdefault("id", str(name))
                record["status"] = "available" if state == "connected" else state
                record["availability"] = "available" if state == "connected" else "unavailable"
                record.setdefault(
                    "reason",
                    None
                    if state == "connected"
                    else str(value.get("last_error") or "MCP server is not connected"),
                )
                record.setdefault(
                    "remediation",
                    None
                    if state == "connected"
                    else "inspect MCP configuration and reconnect the server",
                )
                mcp.append(record)
                continue
            state = str(value)
            mcp.append(
                {
                    "id": str(name),
                    "status": "available" if state == "connected" else "unavailable",
                    "availability": "available" if state == "connected" else "unavailable",
                    "reason": None if state == "connected" else state,
                    "remediation": None
                    if state == "connected"
                    else "inspect MCP configuration and reconnect the server",
                }
            )
        delegates = self._delegates() if callable(self._delegates) else self._delegates
        delegate_records = (
            [self._availability_record(item, kind="delegate") for item in delegates.list()]
            if delegates is not None
            else []
        )
        if not delegate_records:
            delegate_records = [
                {
                    "status": "unavailable",
                    "availability": "unavailable",
                    "reason": "no host-configured delegate is registered",
                    "remediation": "configure a trusted delegate connector",
                }
            ]
        generated = [
            item
            for item in capabilities
            if str(item["id"]).startswith("synth_") or str(item["id"]).startswith("generated")
        ]
        unavailable = [item for item in toolchains if item["status"] == "unavailable"]
        device_records = [
            self._availability_record(item, kind="device") for item in self._list_devices()
        ]
        device_constraints = [
            {
                **item,
                "availability": item["availability"],
                "remediation": item.get("remediation")
                or (
                    None
                    if item["availability"] == "available"
                    else "configure or attach a supported device adapter"
                ),
            }
            for item in device_records
        ]
        platform_record = {
            "os": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "processor": platform.processor() or None,
            "cpu_count": os.cpu_count(),
            "status": "available",
            "availability": "available",
            "reason": None,
            "remediation": None,
        }
        workspace_exists = bool(workspace is not None and Path(workspace.root).is_dir())
        workspace_record = {
            "id": getattr(workspace, "id", None),
            "execution_backend": getattr(workspace, "execution_backend", None),
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
            "status": "available" if workspace_exists else "unavailable",
            "availability": "available" if workspace_exists else "unavailable",
            "reason": None if workspace_exists else "no workspace context supplied",
            "remediation": None if workspace_exists else "supply an existing workspace root",
        }
        host_inventory = {
            "resources": _host_resource_inventory(workspace.root if workspace else None),
            "memory_available_bytes": _available_memory_bytes(),
            "container_engines": {
                name: shutil.which(name) is not None for name in ("docker", "podman")
            },
            "package_managers": {
                name: shutil.which(name) is not None
                for name in ("uv", "pip", "npm", "pnpm", "cargo")
            },
            "compilers": {
                name: shutil.which(name) is not None
                for name in ("cc", "gcc", "clang", "rustc", "go", "javac")
            },
            "runtimes": {
                name: shutil.which(name) is not None
                for name in ("python", "python3", "node", "deno", "bun")
            },
            "shells": {
                name: shutil.which(name) is not None for name in ("sh", "bash", "zsh", "pwsh")
            },
            "gpu": {
                "nvidia_smi": shutil.which("nvidia-smi") is not None,
                "cuda_visible_devices_configured": bool(os.environ.get("CUDA_VISIBLE_DEVICES")),
            },
            "git": {
                "workspace_repository": bool(workspace_root and (workspace_root / ".git").exists()),
                "remotes": _git_remote_inventory(workspace_root),
            },
            "connectivity": {
                "status": "unknown",
                "reason": "connectivity is policy- and route-dependent; no network probe was requested",
            },
            "credential_references": {
                "status": "opaque",
                "source": "operator configuration",
                "values": [],
            },
        }
        environment_record = (
            {
                **dict(environment),
                "status": "available",
                "availability": "available",
                "reason": None,
                "remediation": None,
            }
            if environment is not None
            else {
                "status": "unavailable",
                "availability": "unavailable",
                "reason": "no workspace environment could be described",
                "remediation": "supply a workspace context",
            }
        )
        passport_status = (
            "AVAILABLE"
            if all(item["status"] == "AVAILABLE" for item in capabilities)
            and model_available
            and filesystem["availability"] == "available"
            and workspace_record["availability"] == "available"
            and any(item["availability"] == "available" for item in backend_records)
            and any(item["availability"] == "available" for item in runtime_records)
            and self._execution is not None
            and not any(
                isinstance(item, Mapping)
                and str(item.get("health") or "").casefold()
                in {"degraded", "failed", "unavailable"}
                for item in subsystem_health.values()
            )
            else "PARTIAL"
        )
        return {
            "kind": "environment_passport",
            "status": passport_status,
            "capabilities": capabilities,
            "platform": platform_record,
            "host_inventory": host_inventory,
            "runtimes": runtime_records,
            "backends": backend_records,
            "toolchains": toolchains,
            "models": {
                "providers": model_provider_names,
                "configured": configured_models,
                **({"provider_readiness": provider_readiness} if provider_readiness else {}),
                "status": (
                    "available"
                    if model_available
                    else ("partial" if configured_models else "unavailable")
                ),
                "availability": "available" if model_available else "unavailable",
                "reason": None if model_available else model_reason,
                "remediation": None
                if model_available
                else "configure a provider and model before submitting agent work",
            },
            "mcp": mcp,
            "delegates": delegate_records,
            "generated_capabilities": generated,
            "dependencies": {
                "status": "available" if not unavailable else "partial",
                "availability": "available" if not unavailable else "unavailable",
                "toolchain_unavailable": [item["name"] for item in unavailable],
                "reason": None
                if not unavailable
                else "one or more declared development/toolchain dependencies are unavailable",
                "remediation": None
                if not unavailable
                else "install the missing tools or use a host with the required toolchain",
            },
            "devices": device_records,
            "device_constraints": device_constraints,
            "network": network,
            "filesystem": filesystem,
            "environment": environment_record,
            "workspace": workspace_record,
            "environment_fingerprint": environment_fingerprint,
            "subsystems": subsystem_health,
        }

    async def _describe_workflow(self, workflow_id: str, *, task_id, project_id, user_id) -> dict:
        if self._workflows is None:
            raise ValueError("workflow reflection is unavailable")
        workflow = await self._workflows.get(
            workflow_id,
            task_id=task_id,
            project_id=project_id,
            user_id=user_id,
        )
        if workflow is None:
            raise KeyError(f"workflow not found: {workflow_id}")
        return {"kind": "workflow", **workflow.to_record()}

    async def _describe_skill(self, skill_id: str) -> dict:
        if self._skills is None:
            raise ValueError("skill reflection is unavailable")
        skills = await self._skills.load_active()
        skill = next((item for item in skills if item.id == skill_id), None)
        if skill is None:
            raise KeyError(f"skill not found: {skill_id}")
        return {
            "kind": "skill",
            "id": skill.id,
            "name": skill.name,
            "description": skill.description,
            "triggers": list(skill.triggers),
            "scope": skill.scope,
            "version": skill.version,
        }


def _available_memory_bytes() -> int | None:
    """Return bounded host memory evidence without invoking a subprocess."""
    try:
        meminfo = Path("/proc/meminfo")
        if meminfo.is_file():
            for line in meminfo.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(pages) * int(page_size)
    except (AttributeError, OSError, ValueError):
        return None


def _host_resource_inventory(workspace_root: str | None) -> dict[str, Any]:
    """Return a small, bounded host-resource passport for planning."""
    resources: dict[str, Any] = {
        "cpu_count": os.cpu_count(),
        "load_average": None,
        "process_count": None,
        "workspace_disk": None,
        "mounts": [],
    }
    try:
        resources["load_average"] = [float(value) for value in os.getloadavg()]
    except (AttributeError, OSError):
        pass
    proc = Path("/proc")
    try:
        if proc.is_dir():
            process_count = 0
            for entry in proc.iterdir():
                if entry.name.isdigit():
                    process_count += 1
                if process_count >= 4096:
                    break
            resources["process_count"] = process_count
            mounts = proc / "mounts"
            if mounts.is_file():
                records: list[dict[str, str]] = []
                for line in mounts.read_text(encoding="utf-8", errors="replace").splitlines()[:64]:
                    fields = line.split()
                    if len(fields) >= 3:
                        records.append(
                            {
                                "target": fields[1][:256],
                                "filesystem": fields[2][:64],
                            }
                        )
                resources["mounts"] = records
    except OSError:
        pass
    if workspace_root:
        try:
            usage = shutil.disk_usage(workspace_root)
            resources["workspace_disk"] = {
                "total_bytes": int(usage.total),
                "free_bytes": int(usage.free),
                "used_bytes": int(usage.used),
            }
        except OSError:
            pass
    return resources


def _git_remote_inventory(workspace_root: Path | None) -> list[dict[str, str]]:
    """List remote identities without exposing embedded credentials."""
    if workspace_root is None or not (workspace_root / ".git").exists():
        return []
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace_root), "remote", "-v"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    remotes: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in (result.stdout or "").splitlines()[:32]:
        fields = line.split()
        if len(fields) < 3:
            continue
        name, raw_url, direction = fields[:3]
        parsed = re.match(
            r"^(?:(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://)?(?:(?:[^/@]+)@)?(?P<host>[^/:]+)(?::\d+)?(?P<path>/.*)?$",
            raw_url,
        )
        safe_url = raw_url
        if parsed and parsed.group("host"):
            safe_url = f"{parsed.group('scheme') + '://' if parsed.group('scheme') else ''}{parsed.group('host')}{parsed.group('path') or ''}"
        key = (name, safe_url)
        if key not in seen:
            remotes.append({"name": name[:128], "url": safe_url[:512], "direction": direction[:16]})
            seen.add(key)
    return remotes


def _result(request, *, ok: bool = True, output: str = "", error: str | None = None):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
    )


__all__ = ["CapabilityReflection"]
