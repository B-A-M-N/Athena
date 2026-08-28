"""Deterministic workflow execution through the canonical dispatcher."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from athena.capabilities.dispatcher import SuspendedCall
from athena.capabilities.registry import validate_schema
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResultStatus,
    DispatchDirectives,
)
from athena.protocol.ids import new_id
from athena.protocol.tasks import CapabilityPolicy, ResourceBudget, WorkspaceSpec
from athena.workflows.models import Workflow
from athena.workflows.runs import (
    WorkflowRunIdentityError,
    WorkflowRunRecoveryRequired,
    environment_fingerprint_async,
    workflow_definition_hash,
    workflow_external_transaction_id,
    workspace_revision_async,
)
from athena.workflows.validation import WorkflowValidator


@dataclass(frozen=True)
class WorkflowResult:
    workflow_id: str
    status: str
    outputs: Mapping[str, Any] = field(default_factory=dict)
    failures: tuple[str, ...] = ()
    suspended: SuspendedCall | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class _StepItemOutcome:
    value: Any = None
    failures: tuple[str, ...] = ()
    suspended: SuspendedCall | None = None
    recovery_required: bool = False
    mutated: bool = False


class WorkflowExecutor:
    """Run workflow steps without introducing a second reasoning loop."""

    def __init__(self, dispatcher, *, resolver=None, run_store=None) -> None:
        self._dispatcher = dispatcher
        self._resolver = resolver
        self._run_store = run_store

    async def run(
        self,
        workflow: Workflow,
        *,
        task_id: str | None,
        workspace: WorkspaceSpec,
        profile: str | None = None,
        session_id: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        task_policy: CapabilityPolicy | None = None,
        task_budget: ResourceBudget | None = None,
        generated_call_depth: int = 0,
        generated_call_chain: tuple[str, ...] = (),
        _execution_limiter: asyncio.Semaphore | None = None,
        run_id: str | None = None,
        parent_call_id: str | None = None,
        parent_workflow_id: str | None = None,
    ) -> WorkflowResult:
        validation = WorkflowValidator(self._resolver).validate(workflow)
        if not validation.ok:
            return WorkflowResult(workflow.id, "invalid", failures=validation.errors)
        input_errors = validate_schema(dict(workflow.input_schema), dict(inputs or {}))
        if input_errors:
            return WorkflowResult(
                workflow.id,
                "invalid",
                failures=("workflow input: " + "; ".join(input_errors),),
            )
        outputs: dict[str, Any] = {}
        failures: list[str] = []
        inputs = dict(inputs or {})
        completed: set[str] = set()
        workspace_changed = False
        active_run_id: str | None = None
        if self._run_store is not None:
            active_run_id, restored, completed, prior_status = await self._run_store.start(
                workflow_id=workflow.id,
                task_id=task_id,
                inputs=inputs,
                run_id=run_id,
                definition_hash=workflow_definition_hash(workflow),
                workspace=workspace,
                workspace_revision=await workspace_revision_async(workspace.root),
                environment_identity=await environment_fingerprint_async(workspace),
                parent_call_id=parent_call_id,
                parent_workflow_id=parent_workflow_id,
            )
            outputs.update(restored)
            if prior_status == "completed":
                return WorkflowResult(workflow.id, "completed", outputs, (), None, active_run_id)
            if prior_status == "suspended":
                # An unresolved approval has a durable continuation owned by
                # the kernel.  Re-entering the workflow cannot reconstruct
                # that continuation safely, so never redispatch the step.
                # The approval recovery path will reconcile the receipt and
                # make a later run resumable.
                return WorkflowResult(
                    workflow.id,
                    "suspended",
                    outputs,
                    ("workflow run is awaiting approval",),
                    None,
                    active_run_id,
                )
            if prior_status == "recovery_required":
                return WorkflowResult(
                    workflow.id,
                    "recovery_required",
                    outputs,
                    ("workflow run requires durable recovery",),
                    None,
                    active_run_id,
                )
        execution_limiter = _execution_limiter
        if execution_limiter is None and task_budget is not None:
            execution_limiter = asyncio.Semaphore(max(1, int(task_budget.max_parallel_executions)))
        pending = {step.id: step for step in workflow.steps}
        ready_width = max(
            1,
            int(task_budget.max_parallel_executions) if task_budget is not None else 16,
        )
        while pending:
            ready = sorted(
                (step for step in pending.values() if set(step.depends_on).issubset(completed)),
                key=lambda step: step.id,
            )
            if not ready:
                return WorkflowResult(
                    workflow.id,
                    "invalid",
                    outputs,
                    (*failures, "workflow dependency graph made no progress"),
                )
            batch = [step for step in ready if step.id not in completed][:ready_width]
            if not batch:
                break
            outcomes = await asyncio.gather(
                *(
                    self._run_step(
                        step,
                        inputs=inputs,
                        outputs=outputs,
                        task_id=task_id,
                        workspace=workspace,
                        profile=profile,
                        session_id=session_id,
                        task_policy=task_policy,
                        task_budget=task_budget,
                        generated_call_depth=generated_call_depth,
                        generated_call_chain=generated_call_chain,
                        execution_limiter=execution_limiter,
                        run_id=active_run_id,
                        parent_call_id=parent_call_id,
                        parent_workflow_id=parent_workflow_id,
                    )
                    for step in batch
                )
            )
            stop = False
            for step, outcome in zip(batch, outcomes):
                if outcome.recovery_required:
                    if active_run_id is not None:
                        await self._run_store.finish(
                            active_run_id,
                            status="recovery_required",
                            outputs=outputs,
                        )
                    return WorkflowResult(
                        workflow.id,
                        "recovery_required",
                        outputs,
                        outcome.failures or ("workflow step outcome is unknown",),
                        None,
                        active_run_id,
                    )
                if outcome.suspended is not None:
                    if active_run_id is not None:
                        await self._run_store.finish(
                            active_run_id, status="suspended", outputs=outputs
                        )
                    return WorkflowResult(
                        workflow.id,
                        "suspended",
                        outputs,
                        tuple(failures),
                        outcome.suspended,
                        active_run_id,
                    )
                outputs[step.id] = outcome.value
                failures.extend(outcome.failures)
                completed.add(step.id)
                del pending[step.id]
                workspace_changed = workspace_changed or outcome.mutated
                if active_run_id is not None:
                    await self._run_store.mark_step(
                        active_run_id,
                        step.id,
                        status="failed"
                        if outcome.failures
                        else (
                            "skipped"
                            if isinstance(outcome.value, Mapping)
                            and outcome.value.get("status") == "skipped"
                            else "completed"
                        ),
                        output=outcome.value,
                        failures=outcome.failures,
                    )
                await self._emit_progress(
                    task_id=task_id,
                    run_id=active_run_id,
                    completed=len(completed),
                    total=len(workflow.steps),
                    step_id=step.id,
                )
                if outcome.failures and not step.continue_on_error:
                    stop = True
            if active_run_id is not None and any(outcome.mutated for outcome in outcomes):
                await self._run_store.update_workspace_revision(
                    active_run_id,
                    await workspace_revision_async(workspace.root),
                    environment_identity=await environment_fingerprint_async(workspace),
                )
            if stop:
                break
        if workflow.output_schema is not None:
            output_errors = validate_schema(dict(workflow.output_schema), dict(outputs))
            failures.extend("workflow output: " + error for error in output_errors)
        status = "failed" if failures else "completed"
        if active_run_id is not None:
            await self._run_store.finish(
                active_run_id,
                status=status,
                outputs=outputs,
                workspace_revision=(
                    await workspace_revision_async(workspace.root) if workspace_changed else None
                ),
            )
        return WorkflowResult(workflow.id, status, outputs, tuple(failures), None, active_run_id)

    async def _emit_progress(
        self,
        *,
        task_id: str | None,
        run_id: str | None,
        completed: int,
        total: int,
        step_id: str,
    ) -> None:
        emit = getattr(self._dispatcher, "emit_progress", None)
        if emit is None or total <= 0:
            return
        try:
            await emit(
                task_id=task_id,
                call_id=run_id,
                capability_id="workflow",
                value=completed,
                total=total,
                unit="steps",
                message=f"workflow step {step_id} complete ({completed}/{total})",
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            # Progress is an observation; it cannot change workflow truth.
            return

    async def _run_step(
        self,
        step,
        *,
        inputs: Mapping[str, Any],
        outputs: Mapping[str, Any],
        task_id: str | None,
        workspace: WorkspaceSpec,
        profile: str | None,
        session_id: str | None,
        task_policy: CapabilityPolicy | None,
        task_budget: ResourceBudget | None,
        generated_call_depth: int,
        generated_call_chain: tuple[str, ...],
        execution_limiter: asyncio.Semaphore | None,
        run_id: str | None,
        parent_call_id: str | None,
        parent_workflow_id: str | None,
    ) -> _StepItemOutcome:
        """Execute one ready step; independent ready steps are scheduled together."""
        try:
            if step.if_condition is not None and not _evaluate_condition(
                step.if_condition, inputs, outputs
            ):
                return _StepItemOutcome(value={"status": "skipped"})

            items: list[Any] = [None]
            if step.foreach is not None:
                collection = _resolve_reference(step.foreach, inputs, outputs, current_item=None)
                if isinstance(collection, str):
                    try:
                        import json

                        collection = json.loads(collection)
                    except (TypeError, ValueError):
                        pass
                if not isinstance(collection, list):
                    raise ValueError(f"{step.id}: foreach must resolve to a list")
                if len(collection) > step.max_iterations:
                    raise ValueError(
                        f"{step.id}: foreach exceeds max_iterations {step.max_iterations}"
                    )
                items = collection

            async def _run_item(item: Any, item_index: int) -> _StepItemOutcome:
                try:
                    if step.workflow_id:
                        if self._resolver is None:
                            raise ValueError(f"{step.id}: nested workflow resolver unavailable")
                        nested = self._resolver(step.workflow_id)
                        nested_inputs = _resolve_values(dict(step.arguments), inputs, outputs, item)
                        child_run_id = (
                            f"{run_id}:{step.id}:{item_index}" if run_id is not None else None
                        )
                        prepared = None
                        if run_id is not None and self._run_store is not None:
                            prepared = await self._run_store.prepare_step(
                                run_id,
                                step.id,
                                item_index=item_index,
                                capability_id=f"workflow:{step.workflow_id}",
                                arguments=nested_inputs,
                            )
                            if prepared.get("replay"):
                                return _StepItemOutcome(
                                    value=prepared.get("output"),
                                    failures=tuple(prepared.get("failures") or ()),
                                )
                            bind_nested = getattr(self._run_store, "bind_nested_run", None)
                            if bind_nested is not None:
                                await bind_nested(
                                    run_id,
                                    step.id,
                                    item_index,
                                    child_run_id,
                                )
                        nested_result = await self.run(
                            nested,
                            task_id=task_id,
                            workspace=workspace,
                            profile=profile,
                            session_id=session_id,
                            inputs=nested_inputs,
                            task_policy=task_policy,
                            task_budget=task_budget,
                            generated_call_depth=generated_call_depth,
                            generated_call_chain=generated_call_chain,
                            _execution_limiter=execution_limiter,
                            run_id=child_run_id,
                            parent_call_id=parent_call_id,
                            parent_workflow_id=parent_workflow_id,
                        )
                        nested_failures = (
                            tuple(nested_result.failures)
                            if nested_result.status != "completed"
                            else ()
                        )
                        if nested_result.suspended is not None:
                            if run_id is not None and self._run_store is not None:
                                await self._run_store.mark_item_suspended(
                                    run_id,
                                    step.id,
                                    item_index,
                                    approval_id=nested_result.suspended.approval_id,
                                )
                            return _StepItemOutcome(
                                value=dict(nested_result.outputs),
                                failures=nested_failures,
                                suspended=nested_result.suspended,
                            )
                        if nested_result.status == "recovery_required":
                            # The child has an unknown durable side-effect
                            # outcome.  The parent item must remain
                            # incomplete and must never turn that state into
                            # an ordinary failure or a completed item.
                            return _StepItemOutcome(
                                value=dict(nested_result.outputs),
                                failures=nested_failures
                                or (f"{step.id}: nested workflow requires recovery",),
                                recovery_required=True,
                            )
                        if run_id is not None and self._run_store is not None:
                            await self._run_store.mark_item_applied(
                                run_id,
                                step.id,
                                item_index,
                                output=dict(nested_result.outputs),
                                failures=nested_failures,
                            )
                            await self._run_store.mark_item_complete(
                                run_id,
                                step.id,
                                item_index,
                                output=dict(nested_result.outputs),
                                failures=nested_failures,
                            )
                        return _StepItemOutcome(
                            value=dict(nested_result.outputs),
                            failures=nested_failures,
                            mutated=nested_result.status == "completed",
                        )

                    arguments = _resolve_values(dict(step.arguments), inputs, outputs, item)
                    if run_id is not None:
                        transaction_id = workflow_external_transaction_id(
                            run_id,
                            step.id,
                            item_index,
                            step.capability_id or "",
                            arguments,
                        )
                        if transaction_id is not None:
                            arguments = {**arguments, "transaction_id": transaction_id}
                    prepared = None
                    if run_id is not None and self._run_store is not None:
                        prepared = await self._run_store.prepare_step(
                            run_id,
                            step.id,
                            item_index=item_index,
                            capability_id=step.capability_id or "",
                            arguments=arguments,
                        )
                        if prepared.get("replay"):
                            return _StepItemOutcome(
                                value=prepared.get("output"),
                                failures=tuple(prepared.get("failures") or ()),
                            )
                        call_id = str(prepared["call_id"])
                    else:
                        call_id = new_id("workflow-call")
                    if run_id is not None and self._run_store is not None:
                        await self._run_store.mark_item_applying(
                            run_id,
                            step.id,
                            item_index,
                        )
                    request = CapabilityRequest(
                        capability_id=step.capability_id or "",
                        arguments=arguments,
                        task_id=task_id,
                        session_id=session_id,
                        call_id=call_id,
                        origin=(
                            CapabilityRequestOrigin.GENERATED
                            if generated_call_depth
                            else CapabilityRequestOrigin.TRUSTED_ORCHESTRATION
                        ),
                    )

                    async def _dispatch():
                        return await self._dispatcher.dispatch(
                            request,
                            workspace=workspace,
                            profile=profile,
                            task_policy=task_policy,
                            task_budget=task_budget,
                            _generated_call_depth=generated_call_depth,
                            _generated_call_chain=generated_call_chain,
                            _directives=(
                                DispatchDirectives(
                                    workflow_run_id=run_id,
                                    workflow_step_id=step.id,
                                    workflow_item_index=item_index,
                                    workflow_execution_id=(
                                        str(prepared["execution_id"])
                                        if prepared is not None
                                        else None
                                    ),
                                    workflow_parent_call_id=parent_call_id,
                                    workflow_parent_capability_id=(
                                        "workflow" if parent_call_id else None
                                    ),
                                    workflow_id=parent_workflow_id,
                                )
                                if run_id is not None
                                else None
                            ),
                        )

                    if execution_limiter is None:
                        result = await _dispatch()
                    else:
                        async with execution_limiter:
                            result = await _dispatch()
                    if isinstance(result, SuspendedCall):
                        if run_id is not None and self._run_store is not None:
                            await self._run_store.mark_item_suspended(
                                run_id,
                                step.id,
                                item_index,
                                approval_id=result.approval_id,
                                continuation_id=await self._continuation_id(call_id),
                            )
                        return _StepItemOutcome(suspended=result)
                    if result.status is not CapabilityResultStatus.OK:
                        failure = f"{step.id}: {result.error or 'failed'}"
                        mutation_known, mutation_unknown = _mutation_truth(result, arguments)
                        if mutation_unknown or _requires_external_recovery(result, arguments):
                            # Keep the durable item APPLYING.  Marking an
                            # unknown external outcome COMPLETE would make a
                            # later workflow retry redispatch—or silently
                            # continue past—a side effect that was never
                            # reconciled.
                            return _StepItemOutcome(
                                value=result.output,
                                failures=(failure,),
                                recovery_required=True,
                                mutated=mutation_known,
                            )
                        if run_id is not None and self._run_store is not None:
                            await self._run_store.mark_item_applied(
                                run_id,
                                step.id,
                                item_index,
                                output=result.output,
                                failures=(failure,),
                            )
                            await self._run_store.mark_item_complete(
                                run_id,
                                step.id,
                                item_index,
                                output=result.output,
                                failures=(failure,),
                            )
                        return _StepItemOutcome(
                            value=result.output,
                            failures=(failure,),
                            mutated=mutation_known,
                        )
                    if run_id is not None and self._run_store is not None:
                        await self._run_store.mark_item_applied(
                            run_id,
                            step.id,
                            item_index,
                            output=result.output,
                        )
                        await self._run_store.mark_item_complete(
                            run_id,
                            step.id,
                            item_index,
                            output=result.output,
                        )
                    metadata = dict(result.metadata or {})
                    external = metadata.get("external_effect")
                    mutated = bool(metadata.get("mutation")) or (
                        isinstance(external, Mapping)
                        and str(external.get("status") or "")
                        in {
                            "COMPLETED",
                            "APPLIED",
                            "COMPENSATED",
                            "COMPENSATION_VERIFIED",
                        }
                    )
                    return _StepItemOutcome(value=result.output, mutated=mutated)
                except (WorkflowRunRecoveryRequired, WorkflowRunIdentityError) as exc:
                    return _StepItemOutcome(
                        failures=(f"{step.id}: {exc}",),
                        recovery_required=True,
                    )
                except Exception as exc:  # noqa: BLE001 - item failure is data
                    return _StepItemOutcome(failures=(f"{step.id}: {exc}",))

            if step.parallel and step.foreach is not None and len(items) > 1:
                parallel_limit = step.max_parallel
                if task_budget is not None:
                    parallel_limit = min(
                        parallel_limit,
                        max(1, int(task_budget.max_parallel_executions)),
                    )
                step_limiter = asyncio.Semaphore(max(1, parallel_limit))

                async def _bounded(item_index: int, item: Any) -> _StepItemOutcome:
                    async with step_limiter:
                        return await _run_item(item, item_index)

                item_outcomes = list(
                    await asyncio.gather(
                        *(_bounded(item_index, item) for item_index, item in enumerate(items))
                    )
                )
            else:
                item_outcomes = []
                for item_index, item in enumerate(items):
                    outcome = await _run_item(item, item_index)
                    item_outcomes.append(outcome)
                    if (
                        outcome.recovery_required
                        or outcome.suspended is not None
                        or (outcome.failures and not step.continue_on_error)
                    ):
                        break

            failures: list[str] = []
            suspended: SuspendedCall | None = None
            values: list[Any] = []
            recovery_required = False
            mutated = False
            for outcome in item_outcomes:
                values.append(outcome.value)
                failures.extend(outcome.failures)
                recovery_required = recovery_required or outcome.recovery_required
                mutated = mutated or outcome.mutated
                if suspended is None:
                    suspended = outcome.suspended
            if recovery_required:
                recovery_failures = tuple(failures) or (
                    f"{step.id}: durable outcome requires recovery",
                )
                return _StepItemOutcome(
                    value=(values if step.foreach is not None else values[0] if values else None),
                    failures=recovery_failures,
                    recovery_required=True,
                    mutated=mutated,
                )
            if suspended is not None:
                return _StepItemOutcome(
                    value=values[0] if values else None,
                    failures=tuple(failures),
                    suspended=suspended,
                    mutated=mutated,
                )
            return _StepItemOutcome(
                value=(values if step.foreach is not None else values[0] if values else None),
                failures=tuple(failures),
                mutated=mutated,
            )
        except Exception as exc:  # noqa: BLE001 - step failures are results
            return _StepItemOutcome(failures=(f"{step.id}: {exc}",))

    async def _continuation_id(self, call_id: str) -> str | None:
        """Find the durable continuation created for a suspended step."""
        store = getattr(self._dispatcher, "_continuation_store", None)
        if store is None:
            return None
        try:
            for item in await store.pending():
                if item.get("call_id") == call_id:
                    return str(item.get("id") or "") or None
        except Exception:
            return None
        return None


def _resolve_values(
    value: Any, inputs: Mapping[str, Any], outputs: Mapping[str, Any], current_item: Any = None
) -> Any:
    """Resolve small declarative references without allowing code execution."""
    if isinstance(value, Mapping):
        return {str(k): _resolve_values(v, inputs, outputs, current_item) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_values(item, inputs, outputs, current_item) for item in value]
    if value == "$item":
        return current_item
    if isinstance(value, str) and value.startswith("$"):
        return _resolve_reference(value, inputs, outputs, current_item)
    return value


def _requires_external_recovery(result: Any, arguments: Mapping[str, Any]) -> bool:
    """Identify external results that cannot be reduced to an ordinary failure."""
    metadata = getattr(result, "metadata", None) or {}
    external = metadata.get("external_effect")
    if isinstance(external, Mapping):
        status = str(external.get("status") or "")
        if status in {
            "APPLYING",
            "APPLY_FAILED",
            "VERIFYING",
            "VERIFY_FAILED",
            "COMPENSATING",
            "COMPENSATION_SENT",
            "COMPENSATION_FAILED",
            "COMPENSATION_REJECTED",
            "COMPENSATION_VERIFY_FAILED",
            "RECOVERY_REQUIRED",
        }:
            response = external.get("response")
            if status != "APPLY_FAILED" or not isinstance(response, Mapping):
                return True
            try:
                return int(response.get("status") or 0) >= 500
            except (TypeError, ValueError):
                return True
    error = str(getattr(result, "error", None) or "").casefold()
    phase = str(arguments.get("phase") or "").casefold()
    return phase in {"apply", "verify", "compensate"} and (
        "recovery required" in error or "outcome is uncertain" in error
    )


def _mutation_truth(
    result: Any,
    arguments: Mapping[str, Any],
) -> tuple[bool, bool]:
    """Classify mutation evidence before deciding whether failure is retryable."""
    metadata = getattr(result, "metadata", None) or {}
    if bool(metadata.get("mutation_unknown")):
        return False, True
    if "mutation" in metadata:
        return bool(metadata.get("mutation")), False
    external = metadata.get("external_effect")
    if isinstance(external, Mapping):
        status = str(external.get("status") or "")
        if status in {
            "COMPLETED",
            "APPLIED",
            "VERIFIED",
            "COMPENSATED",
            "COMPENSATION_VERIFIED",
        }:
            return True, False
        if status in {"APPLY_REJECTED", "APPLY_FAILED"}:
            response = external.get("response")
            if status == "APPLY_REJECTED":
                return False, False
            if isinstance(response, Mapping):
                try:
                    return False, int(response.get("status") or 0) >= 500
                except (TypeError, ValueError):
                    pass
            return False, True
        if status:
            return False, status in {
                "APPLYING",
                "VERIFYING",
                "COMPENSATING",
                "RECOVERY_REQUIRED",
                "COMPENSATION_SENT",
                "COMPENSATION_FAILED",
                "COMPENSATION_REJECTED",
                "COMPENSATION_VERIFY_FAILED",
            }
    phase = str(arguments.get("phase") or "").casefold()
    return False, phase in {"apply", "compensate"} and bool(arguments.get("transaction_id"))


def _resolve_reference(
    value: str,
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Any],
    current_item: Any,
) -> Any:
    if value == "$item":
        return current_item
    path = value[1:].split(".") if value.startswith("$") else ()
    if not path:
        return value
    if path[0] in {"input", "inputs"}:
        source, offset = inputs, 1
    elif path[0] in {"output", "outputs"}:
        source, offset = outputs, 1
    elif path[0] in inputs:
        # Short references such as $files remain convenient in task-local
        # workflow definitions while still resolving against data only.
        source, offset = inputs, 0
    else:
        return value
    current: Any = source.get(path[offset]) if len(path) > offset else None
    for part in path[offset + 1 :]:
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return value
    return current


def _evaluate_condition(
    condition: str,
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Any],
) -> bool:
    expression = condition.strip()
    for operator in ("==", "!="):
        if operator in expression:
            left, right = expression.split(operator, 1)
            lhs = _resolve_reference(left.strip(), inputs, outputs, None)
            raw_rhs = right.strip()
            if raw_rhs.startswith("$"):
                rhs = _resolve_reference(raw_rhs, inputs, outputs, None)
            else:
                try:
                    import json

                    rhs = json.loads(raw_rhs.lower())
                except (TypeError, ValueError):
                    rhs = raw_rhs.strip("\"'")
            return (lhs == rhs) if operator == "==" else (lhs != rhs)
    return bool(_resolve_reference(expression, inputs, outputs, None))


__all__ = ["WorkflowExecutor", "WorkflowResult"]
