"""Capability reflection: query the effective affordance surface."""

from __future__ import annotations

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)


class CapabilityReflection:
    descriptor = CapabilityDescriptor(
        id="capabilities",
        description=(
            "Reflect on the effective capability fabric: search and describe "
            "available capabilities, inspect dependencies and provenance, "
            "review lifecycle history, and list machinery created this task."
        ),
        input_schema={
            "type": "object", "required": ["operation"],
            "properties": {
                "operation": {"type": "string", "enum": [
                    "search", "describe", "dependencies", "provenance",
                    "history", "created_this_task", "workflows", "skills",
                    "runtimes", "permissions", "devices"]},
                "query": {"type": "string"},
                "capability_id": {"type": "string"},
                "workflow_id": {"type": "string"},
                "skill_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
        effects=frozenset({EffectClass.READ_LOCAL}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(
        self, fabric, *, workflow_store=None, skills_store=None,
        execution_manager=None, device_provider=None, policy_engine=None,
        approval_store=None,
    ) -> None:
        self._fabric = fabric
        self._workflows = workflow_store
        self._skills = skills_store
        self._execution = execution_manager
        self._devices = device_provider
        self._policy = policy_engine
        self._approvals = approval_store

    async def invoke(self, request: CapabilityRequest, **kw) -> CapabilityResult:
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")
        task_id = request.task_id
        context = kw.get("context")
        workspace = getattr(context, "workspace", None)
        project_id = getattr(workspace, "id", None)
        user_id = "athena"
        try:
            if operation == "search":
                value = self._fabric.search(
                    str(args.get("query") or ""), task_id=task_id,
                    project_id=project_id, user_id=user_id,
                    limit=int(args.get("limit") or 20))
                value = await self._search_other_affordances(
                    value, str(args.get("query") or ""), task_id=task_id,
                    project_id=project_id, user_id=user_id,
                    limit=int(args.get("limit") or 20),
                )
            elif operation == "describe":
                if args.get("workflow_id"):
                    value = await self._describe_workflow(
                        str(args["workflow_id"]), task_id=task_id,
                        project_id=project_id, user_id=user_id)
                elif args.get("skill_id"):
                    value = await self._describe_skill(str(args["skill_id"]))
                else:
                    value = self._fabric.describe(
                        str(args.get("capability_id") or ""), task_id=task_id,
                        project_id=project_id, user_id=user_id)
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
                    task_id=task_id, project_id=project_id, user_id=user_id,
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
                    task_id=task_id, project_id=project_id, user_id=user_id,
                    context=context,
                )
            elif operation == "devices":
                value = self._list_devices()
            else:
                return _result(request, ok=False, error=f"unknown operation: {operation}")
            import json
            return _result(request, output=json.dumps(value, default=str))
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return _result(request, ok=False, error=str(exc))

    async def _search_other_affordances(
        self, values: list[dict], query: str, *, task_id: str | None,
        project_id: str | None, user_id: str, limit: int,
    ) -> list[dict]:
        """Add workflow/skill summaries to capability reflection results."""
        values = [{"kind": "capability", **item} for item in values]
        terms = {term.casefold() for term in query.split() if term.strip()}
        if self._workflows is not None:
            workflows = await self._workflows.list(
                task_id=task_id, project_id=project_id, user_id=user_id,
            )
            for workflow in workflows:
                haystack = f"{workflow.id} {workflow.name} {workflow.description}".casefold()
                if not terms or all(term in haystack for term in terms):
                    values.append({
                        "kind": "workflow", "id": workflow.id,
                        "description": workflow.description,
                        "origin": workflow.scope.value,
                        "effects": [],
                    })
        if self._skills is not None:
            for skill in await self._skills.search(query=query, limit=limit):
                values.append({
                    "kind": "skill", "id": skill.id,
                    "description": skill.describe(),
                    "origin": skill.scope,
                    "effects": [],
                })
        return values[:max(limit, 0)]

    async def _list_workflows(self, *, task_id, project_id, user_id,
                              query: str, limit: int) -> list[dict]:
        if self._workflows is None:
            return []
        workflows = await self._workflows.list(
            task_id=task_id, project_id=project_id, user_id=user_id,
        )
        terms = {term.casefold() for term in query.split() if term.strip()}
        return [
            {
                "kind": "workflow", "id": workflow.id,
                "name": workflow.name, "description": workflow.description,
                "scope": workflow.scope.value,
                "steps": len(workflow.steps),
            }
            for workflow in workflows
            if not terms or all(
                term in f"{workflow.id} {workflow.name} {workflow.description}".casefold()
                for term in terms
            )
        ][:max(limit, 0)]

    async def _list_skills(self, *, query: str, limit: int) -> list[dict]:
        if self._skills is None:
            return []
        skills = await self._skills.search(query=query, limit=limit)
        return [
            {
                "kind": "skill", "id": skill.id, "name": skill.name,
                "description": skill.description, "scope": skill.scope,
                "version": skill.version,
            }
            for skill in skills
        ][:max(limit, 0)]

    def _list_runtimes(self) -> list[dict]:
        if self._execution is None:
            return []
        status = getattr(self._execution, "runtime_status", None)
        if callable(status):
            return list(status())
        names = self._execution.available_runtimes()
        return [{"kind": "runtime", "id": name, "available": True}
                for name in names]

    async def _list_permissions(
        self, *, capability_id: str, task_id: str | None,
        project_id: str | None, user_id: str, context=None,
    ) -> list[dict]:
        descriptors = self._fabric.list_descriptors(
            task_id=task_id, project_id=project_id, user_id=user_id,
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
                    getattr(effect, "value", str(effect))
                    for effect in task_policy.effects
                ),
            }

        pending: list[dict] = []
        if self._approvals is not None and task_id is not None:
            for record in await self._approvals.list_pending(task_id):
                # Approval arguments are intentionally omitted: reflection is
                # model-visible and arguments may contain credentials or data.
                pending.append({
                    "approval_id": record.get("id"),
                    "capability_id": record.get("capability_id"),
                    "status": record.get("status"),
                    "created_at": record.get("created_at"),
                })

        grants: list[dict] = []
        manager = getattr(self._policy, "approvals", None)
        if manager is not None:
            for grant in manager.list_active():
                if grant.task_id not in (None, task_id):
                    continue
                grants.append({
                    "approval_id": grant.id,
                    "capability_id": grant.capability,
                    "effect": getattr(grant.effect, "value", grant.effect),
                    "scope": getattr(grant.scope, "value", grant.scope),
                    "resource_pattern": grant.resource_pattern,
                    "task_id": grant.task_id,
                    "session_id": grant.session_id,
                    "expires_at": (
                        grant.expires_at.isoformat() if grant.expires_at else None
                    ),
                })

        workspace = getattr(context, "workspace", None)
        raw_profile = getattr(self._policy, "profile", None)
        profile = getattr(raw_profile, "value", raw_profile)
        permissions = [
            {
                "kind": "permission",
                "capability_id": descriptor.id,
                "declared_effects": sorted(
                    effect.value for effect in descriptor.effects),
                "availability": descriptor.availability.value,
                "task_allowed": not (
                    task_policy is not None
                    and (
                        descriptor.id in task_policy.deny
                        or (bool(task_policy.allow)
                            and descriptor.id not in task_policy.allow)
                    )
                ),
                "task_requires_approval": bool(
                    task_policy is not None and descriptor.id in task_policy.ask
                ),
                "task_effect_ceiling": task_policy_record["effects"]
                if task_policy_record is not None else [],
            }
            for descriptor in descriptors
        ]
        return [{
            "kind": "policy_context",
            "profile": profile,
            "workspace_id": getattr(workspace, "id", None),
            "network_policy": getattr(
                getattr(workspace, "network_policy", None), "value",
                getattr(workspace, "network_policy", None),
            ),
            "task_policy": task_policy_record,
            "pending_approvals": pending,
            "active_grants": grants,
        }, *permissions]

    def _list_devices(self) -> list[dict]:
        """Return registered device adapters without inventing support."""
        if self._devices is None:
            return [{
                "kind": "device_provider",
                "status": "unsupported",
                "reason": "no device provider is configured",
            }]
        value = self._devices() if callable(self._devices) else self._devices
        devices = list(value or ())
        return devices or [{
            "kind": "device_provider",
            "status": "unsupported",
            "reason": "configured device provider returned no adapters",
        }]

    async def _describe_workflow(self, workflow_id: str, *, task_id,
                                 project_id, user_id) -> dict:
        if self._workflows is None:
            raise ValueError("workflow reflection is unavailable")
        workflow = await self._workflows.get(
            workflow_id, task_id=task_id, project_id=project_id, user_id=user_id,
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
            "kind": "skill", "id": skill.id, "name": skill.name,
            "description": skill.description, "triggers": list(skill.triggers),
            "scope": skill.scope, "version": skill.version,
        }


def _result(request, *, ok: bool = True, output: str = "", error: str | None = None):
    return CapabilityResult(
        request.call_id, request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output, error=error,
    )


__all__ = ["CapabilityReflection"]
