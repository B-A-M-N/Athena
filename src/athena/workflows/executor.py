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
)
from athena.protocol.ids import new_id
from athena.protocol.tasks import CapabilityPolicy, ResourceBudget, WorkspaceSpec
from athena.workflows.models import Workflow
from athena.workflows.validation import WorkflowValidator


@dataclass(frozen=True)
class WorkflowResult:
    workflow_id: str
    status: str
    outputs: Mapping[str, Any] = field(default_factory=dict)
    failures: tuple[str, ...] = ()
    suspended: SuspendedCall | None = None


@dataclass(frozen=True)
class _StepItemOutcome:
    value: Any = None
    failures: tuple[str, ...] = ()
    suspended: SuspendedCall | None = None


class WorkflowExecutor:
    """Run workflow steps without introducing a second reasoning loop."""

    def __init__(self, dispatcher, *, resolver=None) -> None:
        self._dispatcher = dispatcher
        self._resolver = resolver

    async def run(
        self, workflow: Workflow, *, task_id: str | None,
        workspace: WorkspaceSpec, profile: str | None = None,
        session_id: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        task_policy: CapabilityPolicy | None = None,
        task_budget: ResourceBudget | None = None,
        _execution_limiter: asyncio.Semaphore | None = None,
    ) -> WorkflowResult:
        validation = WorkflowValidator(self._resolver).validate(workflow)
        if not validation.ok:
            return WorkflowResult(workflow.id, "invalid", failures=validation.errors)
        input_errors = validate_schema(dict(workflow.input_schema), dict(inputs or {}))
        if input_errors:
            return WorkflowResult(
                workflow.id, "invalid",
                failures=("workflow input: " + "; ".join(input_errors),),
            )
        outputs: dict[str, Any] = {}
        failures: list[str] = []
        inputs = dict(inputs or {})
        execution_limiter = _execution_limiter
        if execution_limiter is None and task_budget is not None:
            execution_limiter = asyncio.Semaphore(
                max(1, int(task_budget.max_parallel_executions))
            )
        for step in workflow.steps:
            try:
                if step.if_condition is not None and not _evaluate_condition(
                    step.if_condition, inputs, outputs):
                    outputs[step.id] = {"status": "skipped"}
                    continue

                items = [None]
                if step.foreach is not None:
                    collection = _resolve_reference(
                        step.foreach, inputs, outputs, current_item=None)
                    if isinstance(collection, str):
                        try:
                            import json
                            collection = json.loads(collection)
                        except (TypeError, ValueError):
                            pass
                    if not isinstance(collection, list):
                        raise ValueError(
                            f"{step.id}: foreach must resolve to a list")
                    if len(collection) > step.max_iterations:
                        raise ValueError(
                            f"{step.id}: foreach exceeds max_iterations "
                            f"{step.max_iterations}")
                    items = collection

                async def _run_item(item: Any) -> _StepItemOutcome:
                    try:
                        if step.workflow_id:
                            if self._resolver is None:
                                raise ValueError(
                                    f"{step.id}: nested workflow resolver unavailable")
                            nested = self._resolver(step.workflow_id)
                            nested_inputs = _resolve_values(
                                dict(step.arguments), inputs, outputs, item)
                            nested_result = await self.run(
                                nested, task_id=task_id, workspace=workspace,
                                profile=profile, session_id=session_id,
                                inputs=nested_inputs, task_policy=task_policy,
                                task_budget=task_budget,
                                _execution_limiter=execution_limiter,
                            )
                            return _StepItemOutcome(
                                value=dict(nested_result.outputs),
                                failures=(
                                    tuple(nested_result.failures)
                                    if nested_result.status != "completed"
                                    and nested_result.status != "suspended"
                                    else ()
                                ),
                                suspended=nested_result.suspended,
                            )

                        arguments = _resolve_values(
                            dict(step.arguments), inputs, outputs, item)
                        request = CapabilityRequest(
                            capability_id=step.capability_id or "",
                            arguments=arguments,
                            task_id=task_id,
                            session_id=session_id,
                            call_id=new_id("workflow-call"),
                            origin=CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
                        )

                        async def _dispatch():
                            return await self._dispatcher.dispatch(
                                request, workspace=workspace, profile=profile,
                                task_policy=task_policy, task_budget=task_budget)

                        if execution_limiter is None:
                            result = await _dispatch()
                        else:
                            async with execution_limiter:
                                result = await _dispatch()
                        if isinstance(result, SuspendedCall):
                            return _StepItemOutcome(suspended=result)
                        if result.status is not CapabilityResultStatus.OK:
                            return _StepItemOutcome(
                                value=result.output,
                                failures=(
                                    f"{step.id}: {result.error or 'failed'}",
                                ),
                            )
                        return _StepItemOutcome(value=result.output)
                    except Exception as exc:  # noqa: BLE001 - item failure is data
                        return _StepItemOutcome(failures=(f"{step.id}: {exc}",))

                item_outcomes: list[_StepItemOutcome]
                if step.parallel and step.foreach is not None and len(items) > 1:
                    parallel_limit = step.max_parallel
                    if task_budget is not None:
                        parallel_limit = min(
                            parallel_limit,
                            max(1, int(task_budget.max_parallel_executions)),
                        )
                    step_limiter = asyncio.Semaphore(max(1, parallel_limit))

                    async def _bounded(item: Any) -> _StepItemOutcome:
                        async with step_limiter:
                            return await _run_item(item)

                    item_outcomes = list(await asyncio.gather(
                        *(_bounded(item) for item in items)
                    ))
                else:
                    item_outcomes = []
                    for item in items:
                        outcome = await _run_item(item)
                        item_outcomes.append(outcome)
                        if outcome.suspended is not None:
                            return WorkflowResult(
                                workflow.id, "suspended", outputs,
                                tuple(failures), outcome.suspended)
                        if outcome.failures:
                            if not step.continue_on_error:
                                break

                step_results: list[Any] = []
                suspended: SuspendedCall | None = None
                for outcome in item_outcomes:
                    step_results.append(outcome.value)
                    if outcome.failures:
                        failures.extend(outcome.failures)
                    if suspended is None and outcome.suspended is not None:
                        suspended = outcome.suspended
                if suspended is not None:
                    return WorkflowResult(
                        workflow.id, "suspended", outputs,
                        tuple(failures), suspended)
                outputs[step.id] = (
                    step_results if step.foreach is not None else step_results[0]
                    if step_results else None
                )
                if failures and not step.continue_on_error:
                    break
            except Exception as exc:  # noqa: BLE001 - step failures are results
                failures.append(f"{step.id}: {exc}")
                if not step.continue_on_error:
                    break
        return WorkflowResult(
            workflow.id, "failed" if failures else "completed", outputs,
            tuple(failures), None)


def _resolve_values(value: Any, inputs: Mapping[str, Any],
                    outputs: Mapping[str, Any], current_item: Any = None) -> Any:
    """Resolve small declarative references without allowing code execution."""
    if isinstance(value, Mapping):
        return {str(k): _resolve_values(v, inputs, outputs, current_item)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_values(item, inputs, outputs, current_item)
                for item in value]
    if value == "$item":
        return current_item
    if isinstance(value, str) and value.startswith("$"):
        return _resolve_reference(value, inputs, outputs, current_item)
    return value


def _resolve_reference(
    value: str, inputs: Mapping[str, Any], outputs: Mapping[str, Any],
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
    for part in path[offset + 1:]:
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return value
    return current


def _evaluate_condition(
    condition: str, inputs: Mapping[str, Any], outputs: Mapping[str, Any],
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
