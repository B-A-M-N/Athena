"""The single model/capability reasoning loop selected by ``AgentKernel``.

``AgentKernel`` remains the reasoning authority and owns all collaborators.
This object is only the loop mechanism: it calls the kernel's injected compile,
inference, dispatch, termination, and finalization seams and never constructs
an alternate authority or policy.
"""

from __future__ import annotations

from typing import Any

from athena.protocol.continuations import SuspendedCall
from athena.kernel.dispatch import DispatchResult
from athena.protocol.errors import (
    ContextIntegrityError,
    ProviderError,
    ProviderOutcomeUnknown,
    RequestCancelled,
    TaskBudgetExceeded,
    TaskDeadlineExceeded,
)
from athena.protocol.messages import CapabilityCallBlock
from athena.protocol.tasks import ResourceBudget, TaskResult, TaskSpec, TaskStatus

__all__ = ["ReasoningLoop"]


class ReasoningLoop:
    """Run the one kernel loop against an existing ``AgentKernel``."""

    def __init__(self, kernel: Any) -> None:
        self._k = kernel

    async def run(self, task: TaskSpec, state: Any) -> TaskResult:
        kernel = self._k
        budget = task.resource_budget or ResourceBudget()

        # Relaunched-entry resume (P1-17): a task relaunched after slot
        # release or process restart re-enters here while durably paused.
        entry = await kernel._resume_paused_entry(task, state)
        if entry is not None:
            return entry

        while True:
            try:
                await kernel._lifecycle.assert_runnable(task)
            except RequestCancelled:
                return await kernel._finalize(task, state, TaskStatus.CANCELLED, "task cancelled")
            except TaskBudgetExceeded:
                return await kernel._finalize(
                    task, state, TaskStatus.PARTIAL, "resource budget exhausted"
                )
            await kernel._refresh_runtime_budget(task, state)
            state.iterations += 1
            if kernel._budgets is not None:
                # Iteration usage is checkpointed before any model/provider
                # work. A process restart cannot make an admitted loop look
                # unused to recovery or budget checks.
                kernel._budgets.consume(task.id, iterations=1)
                persist_budget = getattr(kernel._budgets, "_persist_usage", None)
                if persist_budget is not None:
                    await persist_budget(task.id)
            await kernel._emit("TaskIterationStarted", {"iteration": state.iterations}, task)

            if kernel._deadline_passed(task):
                return await kernel._finalize(task, state, TaskStatus.PARTIAL, "deadline exceeded")
            if _budget_exhausted(state, budget):
                return await kernel._finalize(
                    task, state, TaskStatus.PARTIAL, "resource budget exhausted"
                )

            # Consume the durable canonical call before asking the model for
            # another turn; otherwise an approval continuation could be
            # repaired or executed twice.
            resumed = await kernel._resume_durable_continuation(task)
            if isinstance(resumed, SuspendedCall):
                approval_result = await kernel._approval_path(
                    task, state, DispatchResult(suspended=(resumed,))
                )
                if approval_result is not None:
                    return approval_result
                continue

            await kernel._apply_pending_steering(task)

            try:
                compiled = await kernel._compile(task)
            except ContextIntegrityError as exc:
                return await kernel._finalize(
                    task,
                    state,
                    TaskStatus.RECOVERY_REQUIRED,
                    f"canonical context unavailable; recovery required: {exc}",
                )
            for evidence in _compiled_work_evidence(compiled, task.id):
                if evidence.call_id not in {item.call_id for item in state.work_evidence}:
                    state.work_evidence.append(evidence)
            selection = await kernel._select_model(task, compiled, state=state)

            try:
                response = await kernel._invoke(task, state, selection, compiled)
            except RequestCancelled:
                return await kernel._finalize(task, state, TaskStatus.CANCELLED, "task cancelled")
            except TaskDeadlineExceeded:
                return await kernel._finalize(task, state, TaskStatus.PARTIAL, "deadline exceeded")
            except TaskBudgetExceeded as exc:
                return await kernel._finalize(task, state, TaskStatus.PARTIAL, str(exc))
            except ProviderOutcomeUnknown as exc:
                partial_output = exc.data.get("partial_output")
                if partial_output is not None:
                    await kernel._emit(
                        "DiagnosticsProduced",
                        {
                            "kind": "incomplete_model_stream",
                            "partial_output": partial_output,
                        },
                        task,
                    )
                attempt_id = state.inference_attempt_id or "unknown"
                return await kernel._finalize(
                    task,
                    state,
                    TaskStatus.RECOVERY_REQUIRED,
                    "provider outcome unknown; recovery required before retry "
                    f"(attempt={attempt_id}, provider={state.provider or 'unknown'}, "
                    f"request={state.request_id or 'unknown'}): {exc}",
                )
            except ProviderError as exc:
                partial_output = exc.data.get("partial_output")
                if partial_output is not None:
                    await kernel._emit(
                        "DiagnosticsProduced",
                        {
                            "kind": "provider_stream_failure",
                            "partial_output": partial_output,
                        },
                        task,
                    )
                return await kernel._finalize(task, state, TaskStatus.FAILED, "model unavailable")
            except Exception as exc:  # rationale: kernel boundary preserves truthful terminal state
                return await kernel._finalize(
                    task, state, TaskStatus.FAILED, f"kernel failure: {exc}"
                )

            calls = [block for block in response.blocks if isinstance(block, CapabilityCallBlock)]
            if calls:
                # Persist the assistant turn before dispatch: provider history
                # requires the tool call to precede its result.
                await kernel._append_response(task, response)
                outcome = await kernel._dispatch(task, state, response, calls)
                if outcome is not None:
                    return outcome
                continue

            decision = await kernel._termination.evaluate(
                task,
                response,
                iterations=state.iterations,
                max_iterations=budget.max_agent_iterations,
                budget_exhausted=_budget_exhausted(state, budget),
                cancelled=state.cancel.is_set(),
                completion_mode=compiled.strategy.completion_mode,
                work_evidence=tuple(state.work_evidence),
                recovery_pending=bool(
                    state.generated_recovery_pending or state.speculative_recovery_pending
                ),
            )
            if decision.terminal:
                await kernel._append_final_response(task, response)
                return await kernel._finalize_decision(task, state, decision)

            await kernel._append_response(task, response)


def _budget_exhausted(state: Any, budget: ResourceBudget) -> bool:
    """Keep loop admission checks local to the loop mechanism."""
    from athena.kernel.kernel import _budget_exhausted as _impl

    return _impl(state, budget)


def _compiled_work_evidence(compiled: Any, task_id: str) -> list[Any]:
    """Recover durable work evidence from the freshly compiled context."""
    from athena.kernel.kernel import _compiled_work_evidence as _impl

    return _impl(compiled, task_id)
