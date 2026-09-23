"""Inference broker — routing, provider attempts, cost accounting (P1-10).

Extracted from AgentKernel. Mechanism, not a second authority: every model
selection, provider call, fallback attempt, usage row, and budget
reservation resolves through the bound :class:`AgentKernel` instance
(``self._k``), so a delegate call observes exactly the instance-attribute
patches the method would have observed living on the kernel. The kernel
remains the only component that decides WHEN inference happens; this module
holds HOW one inference attempt is selected, attempted, metered, and
reconciled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Mapping

from athena.context.compiler import CompiledContext
from athena.kernel.inference_retry import invoke_with_retries
from athena.kernel.inference_stream import consume_provider_stream
from athena.kernel.inference_identity import _request_fingerprint  # noqa: F401
from athena.kernel.inference_prefix import observe_prefix
from athena.kernel.route_metadata import inference_metadata, replay_metadata
from athena.models.tokens import ModelTokenEstimator
from athena.models.router import ModelSelection
from athena.protocol.errors import (
    TaskBudgetExceeded,
)
from athena.protocol.models import ModelRequirements
from athena.protocol.models import (
    ModelDelta,
    ModelRequest,
    ModelResponse,
)
from athena.protocol.tasks import (
    TaskSpec,
)


from athena.kernel.utility_inference import UtilityInferenceRunner
from athena.kernel.tokens import (
    actual_model_cost as _actual_model_cost,
    estimate_input_tokens as _estimate_input_tokens,
    input_tokens_of as _input_tokens_of,
    output_tokens_of as _output_tokens_of,
    worst_case_cost as _worst_case_cost,
)


def _escalated_quality_floor(*a, **kw):
    from athena.kernel.kernel import _escalated_quality_floor as _impl

    return _impl(*a, **kw)


if TYPE_CHECKING:
    from athena.kernel.kernel import AgentKernel, RunState

__all__ = ["InferenceBroker"]

_logger = logging.getLogger("athena.kernel")

@dataclass(frozen=True)
class _AttemptBudget:
    remaining: dict[str, Any] | None
    worst_cost: Decimal | None
    input_estimate: int | None


class InferenceBroker:
    """Per-attempt inference mechanism owned by AgentKernel."""

    def __init__(self, kernel: AgentKernel) -> None:
        self._k = kernel
        # Module constant bound lazily so the broker can be imported from the
        # partially-initialized kernel module without an import cycle.
        from athena.kernel.kernel import _FALLBACK_ATTEMPTS

        self._fallback_attempts = _FALLBACK_ATTEMPTS

    async def _fault_point(self, name: str) -> None:
        hook = getattr(self._k, "_inference_fault_injector", None)
        if hook is None:
            return
        result = hook(name)
        import asyncio

        if asyncio.iscoroutine(result):
            await result

    async def _reconcile_receipt(
        self,
        task: TaskSpec,
        receipt: Mapping[str, Any],
        *,
        response: ModelResponse,
        request: ModelRequest,
        estimator: ModelTokenEstimator,
        selection: ModelSelection,
    ) -> None:
        """Finish durable budget/provider accounting before replay returns."""
        attempt_id = str(receipt.get("attempt_id") or "")
        if not attempt_id:
            return
        budget_done = self._k._budgets is None or bool(receipt.get("budget_accounted_at"))
        usage_done = self._k._provider_usage_store is None or bool(
            receipt.get("provider_usage_completed_at")
        )
        if budget_done and usage_done:
            return
        input_tokens = int(
            receipt.get("actual_input_tokens")
            or _input_tokens_of(response, request, estimator=estimator)
        )
        output_tokens = int(receipt.get("actual_output_tokens") or _output_tokens_of(response))
        raw_cost = receipt.get("actual_cost")
        actual_cost = (
            Decimal(str(raw_cost))
            if raw_cost not in (None, "")
            else _actual_model_cost(selection.info, response, request, estimator=estimator)
        )
        response_store = getattr(self._k, "_model_response_store", None)
        if response_store is not None:
            await response_store.set_actual_usage(
                attempt_id=attempt_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=actual_cost,
            )
        raw_reserved = receipt.get("reservation_amount")
        reserved = Decimal(str(raw_reserved)) if raw_reserved not in (None, "") else Decimal("0")
        if self._k._budgets is not None:
            await self._k._budgets.apply_model_accounting(
                task.id,
                attempt_id,
                reserved=reserved,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                actual_cost=actual_cost,
                reservation_id=attempt_id,
            )
            await self._fault_point("budget-charge")
            await self._fault_point("budget-checkpoint")
            if response_store is not None:
                await response_store.mark_budget_accounted(attempt_id=attempt_id)
        usage_id = str(receipt.get("provider_usage_id") or "")
        if self._k._provider_usage_store is not None:
            if not usage_id:
                # The response receipt can outlive a crash in the bookkeeping
                # window between the provider call and its usage row. Reuse
                # the durable attempt ID so recovery creates at most one row.
                usage_id = await self._k._provider_usage_store.record_attempt(
                    provider=selection.provider,
                    model=selection.model,
                    task_id=task.id,
                    session_id=task.session_id,
                    metadata={"state": "recovered", "attempt_id": attempt_id},
                    usage_id=attempt_id,
                )
                if response_store is not None:
                    await response_store.set_provider_usage_id(
                        attempt_id=attempt_id,
                        provider_usage_id=usage_id,
                    )
                    await response_store.mark_provider_usage_started(attempt_id=attempt_id)
            await self._k._provider_usage_store.record_completion(
                usage_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=str(actual_cost) if actual_cost is not None else None,
                metadata={"state": "success", "attempt_id": attempt_id},
            )
            if response_store is not None:
                await response_store.mark_provider_usage_started(attempt_id=attempt_id)
            await self._fault_point("usage-completion")
            if response_store is not None:
                await response_store.mark_provider_usage_completed(attempt_id=attempt_id)
        if response_store is not None:
            await response_store.mark_accounting_applied(
                attempt_id=attempt_id,
            )

    async def _select_model(
        self,
        task: TaskSpec,
        compiled: CompiledContext,
        *,
        state: RunState | None = None,
        exclude: frozenset[str | tuple[str, str]] = frozenset(),
        relax_context: bool = False,
    ) -> ModelSelection:
        caps = set(compiled.requirements.required_capabilities)

        requirements = ModelRequirements(
            required_capabilities=frozenset(caps),
            estimated_input_tokens=getattr(compiled.requirements, "estimated_input_tokens", 0),
            minimum_context_window_tokens=(
                None
                if relax_context
                else getattr(compiled.requirements, "minimum_context_window_tokens", None)
            ),
            requested_output_tokens=getattr(compiled.requirements, "requested_output_tokens", None),
        )
        # Quality floor (P1-16): the task policy's declared floor is the
        # base; a run that keeps needing tool-input corrections escalates
        # one tier for its remaining turns — a cheap model that cannot
        # produce well-formed calls costs more in retries than a stronger
        # model costs in tokens.
        policy = task.model_policy
        escalated = _escalated_quality_floor(policy, state)
        if escalated is not policy:
            policy = escalated
        return await self._k._router.select(
            policy=policy,
            requirements=requirements,
            exclude=exclude,
        )

    async def _budget_preflight(
        self,
        task: TaskSpec,
        request: ModelRequest,
        *,
        selection: ModelSelection,
        estimator: ModelTokenEstimator,
        effective_policy,
    ) -> tuple[ModelRequest, Decimal | None, dict[str, Any] | None]:
        """Bound tokens and reserve-ready worst cost before durable receipts."""
        if self._k._budgets is None:
            return request, None, None
        remaining = await self._k._budgets.remaining(task.id)
        input_estimate = _estimate_input_tokens(request, estimator=estimator)
        input_remaining = remaining.get("input_tokens")
        if input_remaining is not None and input_estimate is None:
            raise TaskBudgetExceeded(
                "model tokenization cannot be bounded safely under hard input budget"
            )
        if (
            input_remaining is not None
            and input_estimate is not None
            and input_estimate > input_remaining
        ):
            raise TaskBudgetExceeded(
                f"model request needs about {input_estimate} input tokens but only "
                f"{input_remaining} remain"
            )
        output_remaining = remaining.get("output_tokens")
        if output_remaining is not None and output_remaining <= 0:
            raise TaskBudgetExceeded("model output-token budget exhausted before provider call")
        if output_remaining is not None:
            request = replace(
                request,
                max_tokens=(
                    output_remaining
                    if request.max_tokens is None
                    else min(request.max_tokens, output_remaining)
                ),
            )
        worst_cost = _worst_case_cost(selection.info, request, estimator=estimator)
        if worst_cost is None and (
            (remaining is not None and remaining.get("cost_usd") is not None)
            or effective_policy.max_cost_usd is not None
        ):
            raise TaskBudgetExceeded(
                "model pricing unknown under hard monetary budget; provider call refused"
            )
        if (
            worst_cost is not None
            and remaining is not None
            and remaining.get("cost_usd") is not None
            and worst_cost > remaining["cost_usd"]
        ):
            raise TaskBudgetExceeded(
                f"bounded model call cost {worst_cost} exceeds remaining "
                f"budget {remaining['cost_usd']} USD"
            )
        if (
            worst_cost is not None
            and effective_policy.max_cost_usd is not None
            and worst_cost > effective_policy.max_cost_usd
        ):
            raise TaskBudgetExceeded(
                f"bounded model call cost {worst_cost} exceeds "
                f"ceiling {effective_policy.max_cost_usd} USD"
            )
        return request, worst_cost, remaining

    async def _prepare_attempt_receipt(
        self,
        task: TaskSpec,
        request: ModelRequest,
        *,
        request_fingerprint: str,
        selection: ModelSelection,
        worst_cost: Decimal | None,
    ) -> dict[str, Any]:
        """Prepare the durable attempt receipt and reconcile stale reservations."""
        response_store = getattr(self._k, "_model_response_store", None)
        if response_store is None:
            return {}
        receipt = await response_store.prepare(
            task_id=task.id,
            request_fingerprint=request_fingerprint,
            request_id=request.request_id,
            provider=selection.provider,
            model=selection.model,
            reservation_amount=worst_cost,
            idempotency_semantics=str(request.metadata.get("idempotency_semantics") or "none"),
        )
        if self._k._budgets is not None:
            for stale in await response_store.list_unreleased_reservations(task.id):
                stale_amount = Decimal(str(stale.get("reservation_amount") or "0"))
                if stale_amount > 0:
                    await self._k._budgets.release_model_cost(
                        task.id,
                        stale_amount,
                        reservation_id=str(stale["attempt_id"]),
                    )
                await response_store.mark_attempt_reservation_released(str(stale["attempt_id"]))
        return receipt

    @staticmethod
    def _apply_receipt_identity(request: ModelRequest, receipt: Mapping[str, Any]) -> ModelRequest:
        """Adopt durable request/idempotency identity prepared by the store."""
        stored_request_id = str(receipt.get("request_id") or "")
        if stored_request_id and stored_request_id != request.request_id:
            request = replace(request, request_id=stored_request_id)
        idempotency_key = str(receipt.get("idempotency_key") or receipt.get("attempt_id") or "")
        if idempotency_key and request.metadata.get("idempotency_key") != idempotency_key:
            request = replace(
                request,
                metadata={**dict(request.metadata), "idempotency_key": idempotency_key},
            )
        return request

    async def _reserve_attempt_budget(
        self,
        task: TaskSpec,
        *,
        request: ModelRequest,
        receipt: Mapping[str, Any],
        worst_cost: Decimal | None,
    ) -> bool:
        """Re-assert the idempotent reservation and persist its receipt marker."""
        response_store = getattr(self._k, "_model_response_store", None)
        if self._k._budgets is None or worst_cost is None:
            return False
        reservation_id = str(receipt.get("attempt_id") or request.request_id)
        await self._k._budgets.reserve_model_cost(
            task.id,
            worst_cost,
            reservation_id=reservation_id,
        )
        if response_store is not None and not receipt.get("reservation_applied_at"):
            await response_store.mark_reservation_applied(attempt_id=str(receipt["attempt_id"]))
        return True

    async def _invoke(
        self,
        task: TaskSpec,
        state: RunState,
        selection: ModelSelection,
        compiled: CompiledContext,
        *,
        inference_kind: str | None = None,
    ) -> ModelResponse:
        return await invoke_with_retries(
            self,
            task,
            state,
            selection,
            compiled,
            inference_kind=inference_kind,
        )

    def _inference_metadata(self, selection: ModelSelection) -> dict[str, Any]:
        return inference_metadata(self._k, selection)

    async def _attempt_metadata(
        self, task: TaskSpec, compiled: CompiledContext, selection: ModelSelection
    ) -> dict[str, Any]:
        """Collect cache/replay metadata once for one provider attempt."""
        cache_metadata = await self._k._observe_prefix(task, compiled, selection)
        if cache_metadata.get("boundary") is not None:
            await self._k._emit("CacheBoundary", cache_metadata["boundary"], task)
        replay_metadata = self._k._replay_metadata(compiled, selection)
        if replay_metadata.get("boundary") is not None:
            await self._k._emit(
                "InferenceReplayBoundary",
                {
                    "boundary": replay_metadata["boundary"],
                    "provider": selection.provider,
                    "model": selection.model,
                },
                task,
            )
        return {**cache_metadata, **replay_metadata}

    async def _observe_prefix(
        self, task: TaskSpec, compiled: CompiledContext, selection: ModelSelection
    ) -> dict[str, Any]:
        return await observe_prefix(self, task, compiled, selection)

    def _replay_metadata(
        self,
        compiled: CompiledContext,
        selection: ModelSelection,
    ) -> dict[str, Any]:
        return replay_metadata(self._k, compiled, selection)

    async def _consume(
        self,
        task: TaskSpec,
        state: RunState,
        provider,
        request: ModelRequest,
        *,
        estimator: ModelTokenEstimator | None = None,
        request_fingerprint: str | None = None,
        attempt_id: str | None = None,
    ) -> ModelResponse:
        return await consume_provider_stream(
            self,
            task,
            state,
            provider,
            request,
            estimator=estimator,
            request_fingerprint=request_fingerprint,
            attempt_id=attempt_id,
        )

    async def utility_inference(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        role: str = "summarizer",
        task_id: str | None = None,
        session_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        budget_task_id: str | None = None,
    ) -> str | None:
        """Delegate best-effort auxiliary mechanics to the focused runner."""
        return await UtilityInferenceRunner(self).run(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            role=role,
            task_id=task_id,
            session_id=session_id,
            metadata=metadata,
            budget_task_id=budget_task_id,
        )

    async def _relay_delta(self, task: TaskSpec, delta: ModelDelta) -> None:
        if delta.reasoning:
            await self._k._emit("ModelReasoningDelta", {}, task)
        if self._k._token_sink is not None and delta.text:
            await self._k._maybe_await(self._k._token_sink(delta.text))
        if delta.text:
            await self._k._emit("ModelDelta", {"text": delta.text}, task)

    # ------------------------------------------------------------------ #
    # Capability dispatch path (INV-004)
    # ------------------------------------------------------------------ #
