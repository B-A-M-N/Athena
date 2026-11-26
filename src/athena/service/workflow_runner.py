"""Service-owned workflow execution adapter for declarative pack hooks."""

from __future__ import annotations

from typing import Any, Mapping

from athena.workflows.executor import WorkflowExecutor
from athena.workflows.graph_loader import WorkflowGraphLoader
from athena.protocol.workflows import WorkflowRunResult


class ServiceWorkflowRunner:
    """Resolve, validate, and execute a pack-owned workflow graph."""

    def __init__(
        self, *, workflow_store, fabric, dispatch_factory, run_store=None, dispatcher=None
    ) -> None:
        self._workflow_store = workflow_store
        self._fabric = fabric
        self._dispatch_factory = dispatch_factory
        self._run_store = run_store
        # Service composition injects the canonical dispatcher directly so
        # the runner does not need to reach through a kernel shim.
        self._dispatcher = dispatcher

    async def run_declared(self, task: Any, invocation: dict[str, Any]) -> WorkflowRunResult:
        workflow_id = str(invocation.get("workflow_id") or "")
        pack_id = str(invocation.get("pack_id") or "")
        workspace = getattr(task, "workspace", None)
        if not workflow_id or not pack_id:
            raise ValueError("pack hook workflow invocation is incomplete")
        if workspace is None:
            raise ValueError("pack hook workflow requires a workspace")
        workflow = await self._workflow_store.get(
            workflow_id,
            task_id=task.id,
            project_id=workspace.id,
            user_id=None,
        )
        if workflow is None:
            raise ValueError(f"declared pack hook workflow not found: {workflow_id}")
        provenance = dict(workflow.provenance or {})
        if provenance.get("pack_id") != pack_id:
            raise ValueError("pack hook workflow provenance does not match its pack")
        if not workflow.enabled or workflow.lifecycle_state != "ACTIVE":
            raise ValueError("declared pack hook workflow is not active")
        dispatcher = self._dispatcher
        if dispatcher is None:
            raise RuntimeError("pack hook workflow dispatcher is unavailable")
        graph = await WorkflowGraphLoader(self._workflow_store).load(
            workflow,
            task_id=task.id,
            project_id=workspace.id,
            user_id=None,
        )

        def resolver(identifier: str):
            nested = graph.get(identifier)
            if nested is not None:
                return nested
            return self._fabric.executor_for(
                identifier,
                task_id=task.id,
                project_id=workspace.id,
                user_id=None,
            ).descriptor

        event_payload = invocation.get("event_payload")
        inputs = {
            "event": dict(event_payload) if isinstance(event_payload, Mapping) else {},
            "event_id": str(invocation.get("event_id") or ""),
            "hook_id": str(invocation.get("hook_id") or ""),
            "pack_id": pack_id,
        }
        outcome = await WorkflowExecutor(
            dispatcher,
            resolver=resolver,
            run_store=self._run_store,
        ).run(
            graph[workflow.id],
            task_id=task.id,
            workspace=workspace,
            session_id=task.session_id,
            inputs=inputs,
            task_policy=task.capability_policy,
            task_budget=task.resource_budget,
        )
        return WorkflowRunResult(
            workflow_id=workflow.id,
            status=str(outcome.status),
            failures=tuple(str(item) for item in outcome.failures),
            suspended=outcome.suspended,
        )


__all__ = ["ServiceWorkflowRunner", "WorkflowRunResult"]
