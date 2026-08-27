"""Workflow capability: invoke durable declarative compositions."""

from __future__ import annotations

import json

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.workflows.executor import WorkflowExecutor
from athena.workflows.models import Workflow, WorkflowStep
from athena.workflows.validation import WorkflowValidator


class WorkflowCapability:
    descriptor = CapabilityDescriptor(
        id="workflow",
        description=(
            "Compose and run declarative capability workflows. Workflows may "
            "nest, are persisted as data, and execute each step through the "
            "normal dispatcher/policy boundary. Operations: list/describe/create/run."
        ),
        input_schema={
            "type": "object", "required": ["operation"],
            "properties": {
                "operation": {"type": "string", "enum": [
                    "list", "describe", "create", "run"]},
                "workflow_id": {"type": "string"},
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "steps": {"type": "array", "items": {"type": "object"}},
                "inputs": {"type": "object"},
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
        },
        effects=frozenset({
            EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL,
            EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS,
            EffectClass.DELETE, EffectClass.NETWORK_WRITE,
        }),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, store, dispatcher, fabric) -> None:
        self._store = store
        self._dispatcher = dispatcher
        self._fabric = fabric

    async def invoke(self, request: CapabilityRequest, *, context=None, **kw):
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")
        if operation == "list":
            workflows = await self._store.list(
                task_id=request.task_id,
                project_id=getattr(getattr(context, "workspace", None), "id", None),
                user_id="athena",
            )
            return _result(request, output=json.dumps([w.to_record() for w in workflows]))
        workflow_id = str(args.get("workflow_id") or "")
        if operation == "describe":
            workflow = await self._store.get(
                workflow_id,
                task_id=request.task_id,
                project_id=getattr(getattr(context, "workspace", None), "id", None),
                user_id="athena",
            )
            if workflow is None:
                return _result(request, ok=False, error=f"unknown workflow: {workflow_id}")
            return _result(request, output=json.dumps(workflow.to_record()))
        if operation == "create":
            if request.task_id is None:
                return _result(
                    request, ok=False,
                    error="task-scoped workflows require a task_id",
                )
            workflow = Workflow.create(
                name=str(args.get("name") or ""),
                description=str(args.get("description") or ""),
                steps=tuple(WorkflowStep.from_record(step, i)
                            for i, step in enumerate(args.get("steps") or [])),
                task_scope=request.task_id,
                input_schema=dict(args.get("input_schema") or {}),
                output_schema=(
                    dict(args["output_schema"])
                    if args.get("output_schema") is not None else None
                ),
            )
            validation = WorkflowValidator(
                lambda capability_id: self._fabric.executor_for(
                    capability_id, task_id=request.task_id,
                    project_id=getattr(getattr(context, "workspace", None), "id", None)
                ).descriptor
            ).validate(workflow)
            if not validation.ok:
                return _result(request, ok=False, error="; ".join(validation.errors))
            await self._store.save(workflow)
            return _result(request, output=workflow.id,
                           metadata={"workflow_id": workflow.id})
        if operation != "run":
            return _result(request, ok=False, error=f"unknown operation: {operation}")
        if context is None:
            return _result(request, ok=False,
                           error="workflow run requires workspace context")
        workflow = await self._store.get(
            workflow_id,
            task_id=request.task_id,
            project_id=context.workspace.id,
            user_id="athena",
        )
        if workflow is None:
            return _result(request, ok=False, error=f"unknown workflow: {workflow_id}")

        graph = await self._load_graph(
            workflow, task_id=request.task_id, project_id=context.workspace.id,
            user_id="athena",
        )
        def resolver(identifier):
            nested = graph.get(identifier)
            if nested is not None:
                return nested
            return self._fabric.executor_for(
                identifier, task_id=request.task_id,
                project_id=context.workspace.id,
                user_id="athena",
            ).descriptor
        outcome = await WorkflowExecutor(self._dispatcher, resolver=resolver).run(
            graph[workflow.id], task_id=request.task_id,
            workspace=context.workspace, inputs=args.get("inputs"),
        )
        ok = outcome.status == "completed"
        return _result(request, ok=ok, output=json.dumps({
            "workflow_id": outcome.workflow_id, "status": outcome.status,
            "outputs": dict(outcome.outputs), "failures": list(outcome.failures),
        }), error="; ".join(outcome.failures) if not ok else None,
                       metadata={"workflow_id": outcome.workflow_id,
                                 "status": outcome.status})

    async def _load_graph(
        self, root: Workflow, *, task_id: str | None, project_id: str | None,
        user_id: str | None,
    ) -> dict[str, Workflow]:
        graph = {root.id: root}
        pending = [step.workflow_id for step in root.steps if step.workflow_id]
        while pending:
            workflow_id = pending.pop()
            if workflow_id in graph:
                continue
            workflow = await self._store.get(
                workflow_id, task_id=task_id, project_id=project_id,
                user_id=user_id,
            )
            if workflow is None:
                continue
            graph[workflow.id] = workflow
            pending.extend(step.workflow_id for step in workflow.steps if step.workflow_id)
        return graph


def _result(request, *, ok=True, output="", error=None, metadata=None):
    return CapabilityResult(
        request.call_id, request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output, error=error, metadata=dict(metadata or {}),
    )


__all__ = ["WorkflowCapability"]
