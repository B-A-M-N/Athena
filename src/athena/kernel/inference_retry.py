"""Bounded provider retry orchestration for :mod:`inference_broker`."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from athena.context.compiler import CompiledContext
from athena.kernel.inference_attempt import run_provider_attempt
from athena.kernel.inference_fallback import prepare_fallback
from athena.kernel.inference_replay import replay_cached_attempt
from athena.kernel.tokens import is_retryable as _is_retryable
from athena.models.router import ModelSelection
from athena.protocol.errors import (
    ContextOverflow,
    ModelUnavailable,
    ProviderError,
    ProviderOutcomeUnknown,
    RequestCancelled,
)
from athena.models.request_bounds import provider_request_upper_bound
from athena.protocol.ids import new_id
from athena.protocol.models import ModelResponse
from athena.protocol.tasks import TaskSpec

if TYPE_CHECKING:
    from athena.kernel.kernel import RunState


def _check_provider_input_allowance(
    provider: Any,
    request,
    selection: ModelSelection,
    compiled: CompiledContext,
    *,
    token_upper_bound_per_byte: int | None,
) -> None:
    """Reject adapter-expanded input before creating a durable attempt."""
    context_limit = selection.info.context_limit
    if context_limit is None:
        return
    wire_bound = provider_request_upper_bound(
        provider,
        request,
        token_upper_bound_per_byte=token_upper_bound_per_byte,
    )
    if wire_bound is None:
        return
    requested_output = getattr(compiled.requirements, "requested_output_tokens", None) or 0
    output_reserve = max(int(request.max_tokens or 0), int(requested_output))
    input_allowance = max(0, int(context_limit) - output_reserve)
    if wire_bound > input_allowance:
        raise ContextOverflow(
            "provider-formatted model request exceeds the selected model input allowance: "
            f"{wire_bound} > {input_allowance} tokens"
        )


async def _preflight_request(
    broker: Any,
    task: TaskSpec,
    request,
    selection: ModelSelection,
    estimator,
    effective_policy,
    provider: Any,
    compiled: CompiledContext,
):
    request, worst_cost, remaining = await broker._budget_preflight(
        task,
        request,
        selection=selection,
        estimator=estimator,
        effective_policy=effective_policy,
    )
    _check_provider_input_allowance(
        provider,
        request,
        selection,
        compiled,
        token_upper_bound_per_byte=estimator.token_upper_bound_per_byte,
    )
    return request, worst_cost, remaining


async def invoke_with_retries(
    broker: Any,
    task: TaskSpec,
    state: RunState,
    selection: ModelSelection,
    compiled: CompiledContext,
    *,
    inference_kind: str | None = None,
) -> ModelResponse:
    """Run bounded model attempts and prepare the next fallback candidate.

    This mechanism owns only retry eligibility and attempt sequencing. The
    broker continues to own provider calls, receipts, accounting, and the
    injected kernel remains the authority for model selection decisions.
    """
    role = getattr(task.model_policy, "role", None) or "primary"
    last_err: ProviderError | None = None
    attempted: set[tuple[str, str]] = set()
    selection_for_attempt = selection
    compiled_for_attempt = compiled
    effective_policy = broker._k._router.effective_policy(task.model_policy)
    max_attempts = min(broker._fallback_attempts, effective_policy.max_model_attempts)
    for attempt in range(max_attempts):
        if state.cancel.is_set():
            raise RequestCancelled("task cancelled")
        pair = (selection_for_attempt.provider, selection_for_attempt.model)
        if pair in attempted:
            raise last_err or ModelUnavailable(
                f"no candidate model excludes failed model selections {sorted(attempted)}"
            )
        provider = broker._k._registry.provider_for(selection_for_attempt.provider)
        attempt_metadata = await broker._attempt_metadata(
            task, compiled_for_attempt, selection_for_attempt
        )
        request = compiled_for_attempt.to_request(
            provider=selection_for_attempt.provider,
            model=selection_for_attempt.model,
            request_id=new_id("call"),
            metadata={
                "task_id": task.id,
                "session_id": task.session_id,
                **broker._inference_metadata(selection_for_attempt),
                **attempt_metadata,
            },
        )
        state.request_id = request.request_id
        state.provider = selection_for_attempt.provider
        effective_policy = broker._k._router.effective_policy(task.model_policy)
        model_profile = broker._k._registry.model_profile_for(
            selection_for_attempt.provider, selection_for_attempt.model
        )
        from athena.models.tokens import ModelTokenEstimator

        token_estimator = ModelTokenEstimator.from_profile(model_profile)
        try:
            request, worst_cost, _remaining = await _preflight_request(
                broker, task, request, selection_for_attempt, token_estimator,
                effective_policy, provider, compiled_for_attempt
            )
        except ContextOverflow as exc:
            last_err = exc
            if attempt >= max_attempts - 1:
                raise
            attempted.add(pair)
            selection_for_attempt, compiled_for_attempt = await prepare_fallback(
                broker._k, task, compiled_for_attempt,
                attempted=frozenset(attempted), error=exc
            )
            continue
        from athena.kernel.inference_broker import _request_fingerprint

        request_fingerprint = _request_fingerprint(
            task,
            request,
            inference_kind=inference_kind,
            attempt=attempt,
        )
        response_store = getattr(broker._k, "_model_response_store", None)
        receipt = await broker._prepare_attempt_receipt(
            task,
            request,
            request_fingerprint=request_fingerprint,
            selection=selection_for_attempt,
            worst_cost=worst_cost,
        )
        state.inference_attempt_id = str(receipt.get("attempt_id") or "") or None
        request = broker._apply_receipt_identity(request, receipt)
        outcome_status = str(receipt.get("provider_outcome_status") or "").casefold()
        if outcome_status not in {"", "pending", "known", "failed"}:
            raise ProviderOutcomeUnknown(
                f"provider outcome requires reconciliation before retrying: {outcome_status}"
            )
        if response_store is not None:
            cached_response = response_store.response_from_row(receipt)
            if cached_response is not None:
                return await replay_cached_attempt(
                    broker,
                    task,
                    state,
                    receipt=receipt,
                    request=request,
                    request_fingerprint=request_fingerprint,
                    response=cached_response,
                    estimator=token_estimator,
                    selection=selection_for_attempt,
                    attempt=attempt,
                )
        reservation = await broker._reserve_attempt_budget(
            task,
            request=request,
            receipt=receipt,
            worst_cost=worst_cost,
        )
        if reservation:
            await broker._fault_point("reservation")
        attempt_usage_id: str | None = None
        attempt_started = time.monotonic()
        request_started_payload: dict[str, Any] = {
            "provider": selection_for_attempt.provider,
            "model": selection_for_attempt.model,
            "provider_profile_id": request.metadata.get("provider_profile_id"),
            "prefix_fingerprint": request.metadata.get("prefix_fingerprint"),
            "role": role,
            "attempt_index": attempt,
        }
        if inference_kind is not None:
            request_started_payload["subturn"] = True
            request_started_payload["inference_kind"] = inference_kind
        request_started_payload["request_id"] = request.request_id
        await broker._k._emit("ModelRequestStarted", request_started_payload, task)
        if broker._k._provider_usage_store is not None:
            try:
                attempt_usage_id = await broker._k._provider_usage_store.record_attempt(
                    provider=selection_for_attempt.provider,
                    model=selection_for_attempt.model,
                    task_id=task.id,
                    session_id=task.session_id,
                    metadata={
                        "inference": dict(broker._inference_metadata(selection_for_attempt)),
                        "role": role,
                        "attempt_index": attempt,
                        "state": "started",
                    },
                    usage_id=str(
                        receipt.get("provider_usage_id") or receipt.get("attempt_id") or ""
                    )
                    or None,
                )
                if response_store is not None:
                    await response_store.set_provider_usage_id(
                        attempt_id=str(receipt["attempt_id"]),
                        provider_usage_id=attempt_usage_id,
                    )
            except Exception as exc:  # rationale: retain provider usage audit evidence
                from athena.kernel.tokens import bookkeeping_failure as _bookkeeping_failure

                _bookkeeping_failure("provider usage attempt record", task, exc)
            await broker._fault_point("provider-usage-start")
        try:
            response, actual_cost = await run_provider_attempt(
                broker,
                task,
                state,
                provider,
                request,
                estimator=token_estimator,
                request_fingerprint=request_fingerprint,
                receipt=receipt,
                selection=selection_for_attempt,
                reservation=reservation,
                worst_cost=worst_cost,
                attempt_usage_id=attempt_usage_id,
                attempt_started=attempt_started,
                role=role,
                attempt=attempt,
                inference_kind=inference_kind,
            )
            state.model_calls += 1
            if actual_cost is None:
                state.cost_known = False
            state.cost += actual_cost or Decimal("0")
            return response
        except ProviderError as exc:
            last_err = exc
            if not _is_retryable(exc):
                raise
            if attempt >= max_attempts - 1:
                break
            attempted.add((selection_for_attempt.provider, selection_for_attempt.model))
            selection_for_attempt, compiled_for_attempt = await prepare_fallback(
                broker._k,
                task,
                compiled_for_attempt,
                attempted=frozenset(attempted),
                error=exc,
            )
    raise last_err or ModelUnavailable("no model available")
