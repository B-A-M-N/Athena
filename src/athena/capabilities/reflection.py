"""Capability reflection: query the effective affordance surface."""

from __future__ import annotations
from athena.capabilities.operations import native_descriptor
from athena.capabilities.reflection_passport_assembly import build_environment_passport
from athena.affordances.discovery import EXPLICIT_REFLECTION_SEARCH

from collections.abc import Mapping
import re
from typing import Any

from athena.protocol.capabilities import (
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.tasks import WorkspaceSpec


class CapabilityReflection:
    descriptor = native_descriptor(
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
                    await self._explain_availability(
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
        from athena.capabilities.reflection_permissions import list_permissions

        return await list_permissions(
            self,
            capability_id=capability_id,
            task_id=task_id,
            project_id=project_id,
            user_id=user_id,
            context=context,
        )

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

    async def _explain_availability(
        self,
        capability_id: str,
        capability_arguments: dict,
        *,
        task_id: str | None,
        project_id: str | None,
        user_id: str | None,
        context=None,
    ) -> dict:
        from athena.capabilities.reflection_availability import explain_availability

        return await explain_availability(
            self,
            capability_id,
            capability_arguments,
            task_id=task_id,
            project_id=project_id,
            user_id=user_id,
            context=context,
        )

    async def _environment_passport(
        self,
        *,
        task_id: str | None,
        project_id: str | None,
        user_id: str | None,
        context=None,
    ) -> dict:
        return await build_environment_passport(
            self,
            task_id=task_id,
            project_id=project_id,
            user_id=user_id,
            context=context,
        )

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
