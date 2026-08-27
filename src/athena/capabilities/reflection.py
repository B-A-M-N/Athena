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
        execution_manager=None, device_provider=None,
    ) -> None:
        self._fabric = fabric
        self._workflows = workflow_store
        self._skills = skills_store
        self._execution = execution_manager
        self._devices = device_provider

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
                value = self._list_permissions(
                    capability_id=str(args.get("capability_id") or ""),
                    task_id=task_id, project_id=project_id, user_id=user_id,
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
        names = self._execution.available_runtimes()
        return [{"kind": "runtime", "id": name, "available": True}
                for name in names]

    def _list_permissions(
        self, *, capability_id: str, task_id: str | None,
        project_id: str | None, user_id: str,
    ) -> list[dict]:
        descriptors = self._fabric.list_descriptors(
            task_id=task_id, project_id=project_id, user_id=user_id,
        )
        if capability_id:
            descriptors = [item for item in descriptors if item.id == capability_id]
        return [
            {
                "kind": "permission",
                "capability_id": descriptor.id,
                "declared_effects": sorted(
                    effect.value for effect in descriptor.effects),
                "availability": descriptor.availability.value,
            }
            for descriptor in descriptors
        ]

    def _list_devices(self) -> list[dict]:
        """Return registered device adapters without inventing support."""
        if self._devices is None:
            return []
        value = self._devices() if callable(self._devices) else self._devices
        return list(value or ())

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
