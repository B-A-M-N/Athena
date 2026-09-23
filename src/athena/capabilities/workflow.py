"""Workflow capability: invoke durable declarative compositions."""

from __future__ import annotations

import json
import inspect
from typing import Any

from athena.protocol.capabilities import (
    CapabilityFailure,
    CapabilityFailureCode,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
    ResourceClass,
)
from athena.workflows.models import Workflow, WorkflowStep
from athena.workflows.validation import WorkflowValidator
from athena.capabilities.operations import native_descriptor
from athena.capabilities.workflow_effects import resolve_workflow_effects
from athena.capabilities.workflow_graph import load_workflow_graph
from athena.capabilities.workflow_workspace import cancel_safe_cleanup, stage_disposable_workspace


class WorkflowCapability:
    descriptor = native_descriptor(
        id="workflow",
        description=(
            "Compose and run declarative capability workflows. Workflows may "
            "nest, are persisted as data, and execute each step through the "
            "normal dispatcher/policy boundary. Operations: "
            "list/describe/create/run/trial/promote/recover. Successful "
            "ordinary runs can be retained as reviewable workflow candidates; "
            "promotion requires distinct successful observations and replay validation."
        ),
        tags=frozenset({"workflow", "procedure", "pipeline", "reusable", "sequence"}),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "additionalProperties": False,
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["list", "describe", "create", "run", "trial", "promote", "recover"],
                },
                "workflow_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "run_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "name": {"type": "string", "minLength": 1, "maxLength": 256},
                "description": {"type": "string", "maxLength": 4000},
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 1000,
                    "items": {"type": "object"},
                },
                "inputs": {"type": "object", "maxProperties": 128},
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "scope": {"type": "string", "enum": ["project", "user"]},
                "step_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "item_index": {"type": "integer", "minimum": 0},
                "transaction_id": {"type": "string", "minLength": 1, "maxLength": 256},
                "resolution": {"type": "string", "enum": ["resume", "abort"]},
            },
            "oneOf": [
                {"properties": {"operation": {"enum": ["list"]}}},
                {"properties": {"operation": {"const": "describe"}}, "required": ["workflow_id"]},
                {"properties": {"operation": {"const": "create"}}, "required": ["name", "steps"]},
                {"properties": {"operation": {"const": "run"}}, "required": ["workflow_id"]},
                {"properties": {"operation": {"const": "trial"}}, "required": ["workflow_id"]},
                {
                    "properties": {"operation": {"const": "promote"}},
                    "required": ["workflow_id", "scope"],
                },
                {
                    "properties": {"operation": {"const": "recover"}},
                    "required": [
                        "workflow_id",
                        "run_id",
                        "step_id",
                        "item_index",
                        "transaction_id",
                        "resolution",
                    ],
                },
            ],
        },
        effects=frozenset(
            {
                EffectClass.READ_LOCAL,
                EffectClass.WRITE_LOCAL,
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
                EffectClass.DELETE,
                EffectClass.NETWORK_WRITE,
            }
        ),
        resources=frozenset({ResourceClass.WORKFLOW}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(
        self,
        store,
        dispatcher,
        fabric,
        run_store=None,
        external_store=None,
        workflow_observer=None,
        *,
        max_trial_files: int = 100_000,
        max_trial_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        if max_trial_files < 1 or max_trial_bytes < 1:
            raise ValueError("workflow trial copy ceilings must be positive")
        self._store = store
        self._dispatcher = dispatcher
        self._fabric = fabric
        self._run_store = run_store
        self._external_store = external_store
        self._workflow_observer = workflow_observer
        self._max_trial_files = max_trial_files
        self._max_trial_bytes = max_trial_bytes

    def _trial_copy_bytes(self, context: Any) -> int:
        task_limit = getattr(getattr(context, "resource_budget", None), "max_artifact_bytes", None)
        if task_limit is None:
            return self._max_trial_bytes
        return min(self._max_trial_bytes, max(1, int(task_limit)))

    async def resolve_operation_effects(
        self,
        arguments,
        *,
        workspace,
        task_id: str | None,
        principal_id: str | None,
    ) -> tuple[EffectClass, ...]:
        operation = str((arguments or {}).get("operation") or "")
        if operation not in {"run", "trial"}:
            return tuple(self.descriptor.resolve_effects(arguments or {}) or ())
        return await resolve_workflow_effects(
            arguments,
            store=self._store,
            fabric=self._fabric,
            load_graph=lambda root, **kwargs: load_workflow_graph(self._store, root, **kwargs),
            workspace=workspace,
            task_id=task_id,
            principal_id=principal_id,
        )

    async def invoke(self, request: CapabilityRequest, *, context=None, **kw):
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")
        principal_id = getattr(context, "principal_id", None)
        if operation == "list":
            workflows = await self._store.list(
                task_id=request.task_id,
                project_id=getattr(getattr(context, "workspace", None), "id", None),
                user_id=principal_id,
            )
            return _result(request, output=json.dumps([w.to_record() for w in workflows]))
        workflow_id = str(args.get("workflow_id") or "")
        if operation == "describe":
            workflow = await self._store.get(
                workflow_id,
                task_id=request.task_id,
                project_id=getattr(getattr(context, "workspace", None), "id", None),
                user_id=principal_id,
            )
            if workflow is None:
                return _result(request, ok=False, error=f"unknown workflow: {workflow_id}")
            return _result(request, output=json.dumps(workflow.to_record()))
        if operation == "recover":
            if (
                request.task_id is None
                or context is None
                or self._run_store is None
                or self._external_store is None
            ):
                return _result(
                    request,
                    ok=False,
                    error="workflow recovery requires task, workspace, and durable stores",
                )
            recover_run_id = str(args.get("run_id") or "")
            step_id = str(args.get("step_id") or "")
            transaction_id = str(args.get("transaction_id") or "")
            try:
                receipt = await self._external_store.get(transaction_id)
                if receipt is None:
                    return _result(
                        request,
                        ok=False,
                        error="external recovery receipt was not found",
                    )
                reconciliation = await self._run_store.reconcile_external_effect(
                    recover_run_id,
                    workflow_id=workflow_id,
                    step_id=step_id,
                    item_index=int(args["item_index"]),
                    transaction_id=transaction_id,
                    receipt=receipt,
                    resolution=str(args.get("resolution") or ""),
                )
            except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                return _result(request, ok=False, error=str(exc))
            return _result(request, output=json.dumps(reconciliation, sort_keys=True))
        if operation == "promote":
            if request.task_id is None or context is None:
                return _result(
                    request,
                    ok=False,
                    error="workflow promotion requires task and workspace context",
                )
            scope = str(args.get("scope") or "")
            if scope == "user" and not principal_id:
                return _result(
                    request, ok=False, error="user workflow promotion requires principal"
                )
            try:
                candidate = await self._store.get(
                    workflow_id,
                    task_id=request.task_id,
                    project_id=context.workspace.id,
                    user_id=principal_id,
                )
                if candidate is None or candidate.scope.value != "candidate":
                    return _result(
                        request,
                        ok=False,
                        error="workflow candidate not found or not owned",
                    )
                if int(candidate.provenance.get("successful_observations") or 0) < 2:
                    return _result(
                        request,
                        ok=False,
                        error="workflow candidate needs two distinct successful task observations",
                    )
                replay = await self._replay_validation(
                    candidate,
                    request=request,
                    context=context,
                )
                if replay["status"] != "completed":
                    return _result(
                        request,
                        ok=False,
                        error="workflow replay validation failed: "
                        + "; ".join(replay.get("failures") or ()),
                        metadata={"replay_validation": replay},
                    )
                promoted = await self._store.promote_candidate(
                    workflow_id,
                    task_id=request.task_id,
                    scope=scope,
                    project_id=context.workspace.id if scope == "project" else None,
                    user_id=principal_id if scope == "user" else None,
                    validation=replay,
                )
            except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                return _result(request, ok=False, error=str(exc))
            if promoted is None:
                return _result(request, ok=False, error="workflow candidate not found or not owned")
            return _result(
                request,
                output=json.dumps(promoted.to_record()),
                metadata={
                    "workflow_id": promoted.id,
                    "scope": scope,
                    "replay_validation": replay,
                },
            )
        if operation == "create":
            if request.task_id is None:
                return _result(
                    request,
                    ok=False,
                    error="task-scoped workflows require a task_id",
                )
            workflow = Workflow.create(
                name=str(args.get("name") or ""),
                description=str(args.get("description") or ""),
                steps=tuple(
                    WorkflowStep.from_record(step, i)
                    for i, step in enumerate(args.get("steps") or [])
                ),
                task_scope=request.task_id,
                input_schema=dict(args.get("input_schema") or {}),
                output_schema=(
                    dict(args["output_schema"]) if args.get("output_schema") is not None else None
                ),
            )
            # Resolve nested workflow references from the same owner-scoped
            # graph used by execution.  Creation must validate composition as
            # data, not reject a valid child merely because it is not a native
            # capability in the fabric.
            graph = await load_workflow_graph(
                self._store,
                workflow,
                task_id=request.task_id,
                project_id=getattr(getattr(context, "workspace", None), "id", None),
                user_id=principal_id,
            )

            def create_resolver(identifier):
                nested = graph.get(identifier)
                if nested is not None:
                    return nested
                return self._fabric.executor_for(
                    identifier,
                    task_id=request.task_id,
                    project_id=getattr(getattr(context, "workspace", None), "id", None),
                ).descriptor

            validation = WorkflowValidator(create_resolver).validate(workflow)
            if not validation.ok:
                return _result(request, ok=False, error="; ".join(validation.errors))
            await self._store.save(workflow)
            return _result(request, output=workflow.id, metadata={"workflow_id": workflow.id})
        if operation not in {"run", "trial"}:
            return _result(request, ok=False, error=f"unknown operation: {operation}")
        if context is None:
            return _result(request, ok=False, error="workflow run requires workspace context")
        workflow = await self._store.get(
            workflow_id,
            task_id=request.task_id,
            project_id=context.workspace.id,
            user_id=principal_id,
        )
        if workflow is None:
            return _result(request, ok=False, error=f"unknown workflow: {workflow_id}")

        graph = await load_workflow_graph(
            self._store,
            workflow,
            task_id=request.task_id,
            project_id=context.workspace.id,
            user_id=principal_id,
        )

        def resolver(identifier):
            nested = graph.get(identifier)
            if nested is not None:
                return nested
            return self._fabric.executor_for(
                identifier,
                task_id=request.task_id,
                project_id=context.workspace.id,
                user_id=principal_id,
            ).descriptor

        trial_root = None
        execution_workspace = context.workspace
        try:
            if operation == "trial":
                execution_workspace, trial_root = await stage_disposable_workspace(
                    context.workspace,
                    identifier=f"trial:{request.call_id}",
                    prefix="athena-workflow-trial-",
                    max_files=self._max_trial_files,
                    max_bytes=self._trial_copy_bytes(context),
                )
            # Keep the workflow package importable on its own.  The capabilities
            # package exports WorkflowCapability, while WorkflowExecutor also
            # depends on the dispatcher exported by that package.
            from athena.workflows.executor import WorkflowExecutor

            run_id = str(args["run_id"]) if args.get("run_id") else None
            inputs = args.get("inputs")
            if run_id is not None and inputs is None and self._run_store is not None:
                persisted = await self._run_store.get(run_id)
                if isinstance(persisted, dict):
                    inputs = persisted.get("inputs")

            outcome = await WorkflowExecutor(
                self._dispatcher,
                resolver=resolver,
                run_store=self._run_store,
            ).run(
                graph[workflow.id],
                task_id=request.task_id,
                workspace=execution_workspace,
                inputs=inputs,
                session_id=request.session_id,
                profile=getattr(context, "autonomy", None),
                task_policy=getattr(context, "capability_policy", None),
                task_budget=getattr(context, "resource_budget", None),
                generated_call_depth=getattr(context, "generated_call_depth", 0),
                generated_call_chain=tuple(getattr(context, "generated_call_chain", ())),
                run_id=run_id,
                parent_call_id=request.call_id,
                parent_workflow_id=workflow.id,
            )
        finally:
            if trial_root is not None:
                await cancel_safe_cleanup(trial_root)
        if (
            operation == "run"
            and self._workflow_observer is not None
            and outcome.status == "completed"
        ):
            try:
                observed = self._workflow_observer(
                    task_id=request.task_id,
                    workflow=workflow,
                    outcome=outcome,
                )
                if inspect.isawaitable(observed):
                    await observed
            except Exception:
                # Learning is post-action enrichment. It cannot change the
                # already-durable workflow result or cause a rerun.
                pass
        ok = outcome.status == "completed"
        if outcome.suspended is not None:
            # Preserve the inner canonical approval continuation. The outer
            # workflow capability is an orchestration envelope, not a second
            # approval authority.
            outcome.suspended.workflow_run_id = outcome.run_id
            outcome.suspended.workflow_id = workflow.id
            outcome.suspended.workflow_parent_request = request
            return outcome.suspended
        return _result(
            request,
            ok=ok,
            output=json.dumps(
                {
                    "workflow_id": outcome.workflow_id,
                    "status": outcome.status,
                    "run_id": outcome.run_id,
                    "outputs": dict(outcome.outputs),
                    "failures": list(outcome.failures),
                    "trial": operation == "trial",
                }
            ),
            error="; ".join(outcome.failures) if not ok else None,
            metadata={
                "workflow_id": outcome.workflow_id,
                "status": outcome.status,
                "trial": operation == "trial",
            },
        )

    async def _replay_validation(
        self,
        workflow: Workflow,
        *,
        request: CapabilityRequest,
        context: Any,
    ) -> dict[str, Any]:
        """Replay a candidate in a disposable workspace before promotion."""
        graph = await load_workflow_graph(
            self._store,
            workflow,
            task_id=request.task_id,
            project_id=context.workspace.id,
            user_id=getattr(context, "principal_id", None),
        )

        def resolver(identifier):
            nested = graph.get(identifier)
            if nested is not None:
                return nested
            return self._fabric.executor_for(
                identifier,
                task_id=request.task_id,
                project_id=context.workspace.id,
                user_id=getattr(context, "principal_id", None),
            ).descriptor

        execution_workspace, trial_root = await stage_disposable_workspace(
            context.workspace,
            identifier=f"replay:{request.call_id}",
            prefix="athena-workflow-replay-",
            max_files=self._max_trial_files,
            max_bytes=self._trial_copy_bytes(context),
        )
        try:
            from athena.workflows.executor import WorkflowExecutor

            outcome = await WorkflowExecutor(
                self._dispatcher,
                resolver=resolver,
            ).run(
                graph[workflow.id],
                task_id=request.task_id,
                workspace=execution_workspace,
                inputs=dict(workflow.provenance.get("representative_inputs") or {}),
                session_id=request.session_id,
                task_policy=getattr(context, "capability_policy", None),
                task_budget=getattr(context, "resource_budget", None),
                generated_call_depth=getattr(context, "generated_call_depth", 0),
                generated_call_chain=tuple(getattr(context, "generated_call_chain", ())),
            )
            return {
                "status": outcome.status,
                "outputs": dict(outcome.outputs),
                "failures": list(outcome.failures),
                "workspace": "disposable",
            }
        finally:
            await cancel_safe_cleanup(trial_root)

def _result(request, *, ok=True, output="", error=None, metadata=None):
    if ok:
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.OK,
            output=output,
            error=error,
            metadata=dict(metadata or {}),
        )
    return CapabilityResult.failure(
        request,
        CapabilityFailure(
            code=CapabilityFailureCode.DOMAIN_REJECTED,
            detail=error or "operation failed",
            stage="invoke",
        ),
        output=output,
        metadata=dict(metadata or {}),
    )


__all__ = ["WorkflowCapability"]
