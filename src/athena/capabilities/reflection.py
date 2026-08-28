"""Capability reflection: query the effective affordance surface."""

from __future__ import annotations

import re

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
    ) -> None:
        self._fabric = fabric
        self._workflows = workflow_store
        self._skills = skills_store
        self._execution = execution_manager
        self._devices = device_provider
        self._policy = policy_engine
        self._approvals = approval_store
        self._health = health_provider

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
        user_id = "athena"
        try:
            if operation == "search":
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
                    else self._environment_passport(
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
        user_id: str,
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
            return list(status())
        names = self._execution.available_runtimes()
        return [{"kind": "runtime", "id": name, "available": True} for name in names]

    async def _list_permissions(
        self,
        *,
        capability_id: str,
        task_id: str | None,
        project_id: str | None,
        user_id: str,
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
        user_id: str,
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
            checks.append(
                {
                    "kind": "network",
                    "status": "restricted" if network_policy == "restricted" else "available",
                    "policy": network_policy or "unknown",
                }
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

    def _environment_passport(
        self,
        *,
        task_id: str | None,
        project_id: str | None,
        user_id: str,
        context=None,
    ) -> dict:
        """Summarize the effective machine/task surface in one graph."""
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
                    "preconditions": item["preconditions"],
                    "checks": item["checks"],
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
        return {
            "kind": "environment_passport",
            "status": "AVAILABLE"
            if all(item["status"] == "AVAILABLE" for item in capabilities)
            else "PARTIAL",
            "capabilities": capabilities,
            "runtimes": self._list_runtimes(),
            "backends": backends,
            "devices": self._list_devices(),
            "environment": environment,
            "workspace": {
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
            },
            "environment_fingerprint": environment_fingerprint,
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


def _result(request, *, ok: bool = True, output: str = "", error: str | None = None):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
    )


__all__ = ["CapabilityReflection"]
