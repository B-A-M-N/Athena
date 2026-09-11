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

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Mapping

from athena.context.compiler import CompiledContext
from athena.models.tokens import ModelTokenEstimator
from athena.models.router import (
    CAP_AUDIO_INPUT,
    CAP_REASONING,
    CAP_TOOLS,
    CAP_VISION,
    ModelSelection,
)
from athena.protocol.errors import (
    ContextOverflow,
    ModelUnavailable,
    ProviderOutcomeUnknown,
    ProviderError,
    RequestCancelled,
    TaskBudgetExceeded,
    TaskDeadlineExceeded,
)
from athena.protocol.ids import new_id
from athena.protocol.models import (
    ModelDelta,
    ModelRequest,
    ModelResponse,
    ModelResponseAccumulator,
)
from athena.protocol.messages import (
    Message,
    Provenance,
    SourceType,
    TrustClass,
    utcnow,
)
from athena.protocol.tasks import (
    TaskSpec,
)


# The kernel module owns these helpers single-sourced; the broker binds them
# lazily to break the kernel <-> broker module cycle.
def _mod():
    from athena.kernel import kernel as m

    return m


def _actual_model_cost(*a, **kw):
    return _mod()._actual_model_cost(*a, **kw)


def _bookkeeping_failure(*a, **kw):
    return _mod()._bookkeeping_failure(*a, **kw)


def _estimate_input_tokens(*a, **kw):
    return _mod()._estimate_input_tokens(*a, **kw)


def _escalated_quality_floor(*a, **kw):
    return _mod()._escalated_quality_floor(*a, **kw)


def _input_tokens_of(*a, **kw):
    return _mod()._input_tokens_of(*a, **kw)


def _worst_case_cost(*a, **kw):
    return _mod()._worst_case_cost(*a, **kw)


def _output_tokens_of(*a, **kw):
    return _mod()._output_tokens_of(*a, **kw)


def _is_retryable(*a, **kw):
    return _mod()._is_retryable(*a, **kw)


if TYPE_CHECKING:
    from athena.kernel.kernel import AgentKernel, RunState

__all__ = ["InferenceBroker"]

_logger = logging.getLogger("athena.kernel")


def _request_fingerprint(
    task: TaskSpec,
    request: ModelRequest,
    *,
    inference_kind: str | None,
    attempt: int,
) -> str:
    """Hash the exact logical provider prompt, excluding random request IDs."""
    from athena.state.sessions import _serialize_block

    payload = {
        "task_id": task.id,
        "kind": inference_kind or "primary",
        "attempt": attempt,
        "provider": request.provider,
        "model": request.model,
        "system": request.system,
        "max_tokens": request.max_tokens,
        "stop": list(request.stop),
        "messages": [
            {
                "role": message.role.value,
                "blocks": [_serialize_block(block) for block in message.blocks],
                "metadata": dict(message.metadata or {}),
            }
            for message in request.messages
        ],
        "capabilities": [vars(capability) for capability in request.capabilities],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class InferenceBroker:
    """Per-attempt inference mechanism owned by AgentKernel."""

    def __init__(self, kernel: AgentKernel) -> None:
        self._k = kernel
        # Module constant bound lazily so the broker can be imported from the
        # partially-initialized kernel module without an import cycle.
        self._fallback_attempts = _mod()._FALLBACK_ATTEMPTS

    async def _fault_point(self, name: str) -> None:
        hook = getattr(self._k, "_inference_fault_injector", None)
        if hook is None:
            return
        result = hook(name)
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
        from athena.models.router import ModelRequirements

        caps: set[str] = set()
        if getattr(compiled.requirements, "needs_tools", False):
            caps.add(CAP_TOOLS)
        if getattr(compiled.requirements, "vision", False):
            caps.add(CAP_VISION)
        if getattr(compiled.requirements, "audio", False):
            caps.add(CAP_AUDIO_INPUT)
        if getattr(compiled.requirements, "reasoning", False):
            caps.add(CAP_REASONING)

        requirements = ModelRequirements(
            required_capabilities=frozenset(caps),
            # The compiler's requirement field is ``minimum_context_tokens``
            # (P0 fix: the old name silently dropped the constraint and let
            # an undersized-context model survive routing).
            minimum_context_tokens=(
                None
                if relax_context
                else getattr(compiled.requirements, "minimum_context_tokens", None)
            ),
            max_output_tokens=getattr(compiled.requirements, "reserved_output", None),
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

    async def _invoke(
        self,
        task: TaskSpec,
        state: RunState,
        selection: ModelSelection,
        compiled: CompiledContext,
        *,
        inference_kind: str | None = None,
    ) -> ModelResponse:
        role = getattr(task.model_policy, "role", None) or "primary"
        last_err: ProviderError | None = None
        # Model-granular fallback (task #12): track failed (provider, model)
        # pairs, NOT provider names, so a failing model on a multi-model
        # provider does not ban that provider's healthy sibling models.
        attempted: set[tuple[str, str]] = set()
        selection_for_attempt = selection
        compiled_for_attempt = compiled
        effective_policy = self._k._router.effective_policy(task.model_policy)
        max_attempts = min(self._fallback_attempts, effective_policy.max_model_attempts)
        for attempt in range(max_attempts):
            if state.cancel.is_set():
                raise RequestCancelled("task cancelled")
            pair = (
                selection_for_attempt.provider,
                selection_for_attempt.model,
            )
            if pair in attempted:
                raise last_err or ModelUnavailable(
                    f"no candidate model excludes failed model selections {sorted(attempted)}"
                )
            provider = self._k._registry.provider_for(selection_for_attempt.provider)
            attempt_metadata = await self._k._attempt_metadata(
                task, compiled_for_attempt, selection_for_attempt
            )
            request = compiled_for_attempt.to_request(
                provider=selection_for_attempt.provider,
                model=selection_for_attempt.model,
                request_id=new_id("call"),
                metadata={
                    "task_id": task.id,
                    "session_id": task.session_id,
                    **self._k._inference_metadata(selection_for_attempt),
                    **attempt_metadata,
                },
            )
            state.request_id = request.request_id
            state.provider = selection_for_attempt.provider
            effective_policy = self._k._router.effective_policy(task.model_policy)
            model_profile = self._k._registry.model_profile_for(
                selection_for_attempt.provider, selection_for_attempt.model
            )
            token_estimator = ModelTokenEstimator.from_profile(model_profile)
            worst_cost = _worst_case_cost(
                selection_for_attempt.info, request, estimator=token_estimator
            )
            remaining = None
            if self._k._budgets is not None:
                remaining = await self._k._budgets.remaining(task.id)
                input_estimate = _estimate_input_tokens(request, estimator=token_estimator)
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
                    raise TaskBudgetExceeded(
                        "model output-token budget exhausted before provider call"
                    )
                if output_remaining is not None:
                    request = replace(
                        request,
                        max_tokens=(
                            output_remaining
                            if request.max_tokens is None
                            else min(request.max_tokens, output_remaining)
                        ),
                    )
                    worst_cost = _worst_case_cost(
                        selection_for_attempt.info, request, estimator=token_estimator
                    )
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
            request_fingerprint = _request_fingerprint(
                task,
                request,
                inference_kind=inference_kind,
                attempt=attempt,
            )
            response_store = getattr(self._k, "_model_response_store", None)
            receipt: dict[str, Any] = {}
            if response_store is not None:
                receipt = await response_store.prepare(
                    task_id=task.id,
                    request_fingerprint=request_fingerprint,
                    request_id=request.request_id,
                    provider=selection_for_attempt.provider,
                    model=selection_for_attempt.model,
                    reservation_amount=worst_cost,
                    idempotency_semantics=str(
                        request.metadata.get("idempotency_semantics") or "none"
                    ),
                )
                state.inference_attempt_id = str(receipt.get("attempt_id") or "") or None
                if self._k._budgets is not None:
                    for stale in await response_store.list_unreleased_reservations(task.id):
                        stale_amount = Decimal(str(stale.get("reservation_amount") or "0"))
                        if stale_amount > 0:
                            await self._k._budgets.release_model_cost(
                                task.id,
                                stale_amount,
                                reservation_id=str(stale["attempt_id"]),
                            )
                        await response_store.mark_attempt_reservation_released(
                            str(stale["attempt_id"])
                        )
                stored_request_id = str(receipt.get("request_id") or "")
                if stored_request_id and stored_request_id != request.request_id:
                    request = replace(request, request_id=stored_request_id)
                idempotency_key = str(
                    receipt.get("idempotency_key") or receipt.get("attempt_id") or ""
                )
                if idempotency_key and request.metadata.get("idempotency_key") != idempotency_key:
                    request = replace(
                        request,
                        metadata={**dict(request.metadata), "idempotency_key": idempotency_key},
                    )
                outcome_status = str(receipt.get("provider_outcome_status") or "").casefold()
                if outcome_status not in {"", "pending", "known", "failed"}:
                    raise ProviderOutcomeUnknown(
                        "provider outcome requires reconciliation before retrying: "
                        f"{outcome_status}"
                    )
                cached_response = response_store.response_from_row(receipt)
                if cached_response is not None:
                    await self._reconcile_receipt(
                        task,
                        {**receipt, "request_fingerprint": request_fingerprint},
                        response=cached_response,
                        request=request,
                        estimator=token_estimator,
                        selection=selection_for_attempt,
                    )
                    state.request_id = cached_response.request_id
                    state.provider = cached_response.provider
                    state.model_calls += 1
                    state.input_tokens += _input_tokens_of(
                        cached_response, request, estimator=token_estimator
                    )
                    state.output_tokens += _output_tokens_of(cached_response)
                    cached_cost = _actual_model_cost(
                        selection_for_attempt.info,
                        cached_response,
                        request,
                        estimator=token_estimator,
                    )
                    if cached_cost is None:
                        state.cost_known = False
                    state.cost += cached_cost or Decimal("0")
                    await self._k._emit(
                        "ModelResponseReplayed",
                        {
                            "provider": cached_response.provider,
                            "model": cached_response.model,
                            "request_id": cached_response.request_id,
                            "attempt_index": attempt,
                        },
                        task,
                    )
                    return cached_response
            reservation = False
            if self._k._budgets is not None and worst_cost is not None:
                # Re-assert the reservation on every recovery attempt.  The
                # receipt marker means the reserve operation committed at
                # least once; it does not prove that an interrupted caller
                # still has an in-memory reservation after cleanup.  The
                # budget ledger makes this operation idempotent by attempt id,
                # so this also repairs a crash between reservation release and
                # the next replay.
                reservation_id = str(receipt.get("attempt_id") or request.request_id)
                await self._k._budgets.reserve_model_cost(
                    task.id,
                    worst_cost,
                    reservation_id=reservation_id,
                )
                if response_store is not None and not receipt.get("reservation_applied_at"):
                    await response_store.mark_reservation_applied(
                        attempt_id=str(receipt["attempt_id"])
                    )
                reservation = True
                await self._fault_point("reservation")
            # One durable row and one inspectable event per actual provider /
            # model attempt. Fallbacks must never overwrite the first row.
            attempt_usage_id: str | None = None
            provider_started = False
            attempt_started = time.monotonic()
            # Inference-kind metadata (P1-8): auxiliary subturns carry the
            # same lifecycle events as primary inference — emitted here, once
            # — plus a kind marker so operators can distinguish them without
            # counting duplicate event pairs.
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
            await self._k._emit("ModelRequestStarted", request_started_payload, task)
            if self._k._provider_usage_store is not None:
                try:
                    attempt_usage_id = await self._k._provider_usage_store.record_attempt(
                        provider=selection_for_attempt.provider,
                        model=selection_for_attempt.model,
                        task_id=task.id,
                        session_id=task.session_id,
                        metadata={
                            "inference": dict(self._k._inference_metadata(selection_for_attempt)),
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
                except Exception as exc:
                    # P1-11: usage evidence must not vanish silently.
                    _bookkeeping_failure("provider usage attempt record", task, exc)
                await self._fault_point("provider-usage-start")
            try:
                provider_started = True
                if self._k._budgets is not None:
                    async with self._k._budgets.model_call_lease(task.id):
                        response = await self._k._consume(
                            task,
                            state,
                            provider,
                            request,
                            estimator=token_estimator,
                            request_fingerprint=request_fingerprint,
                            attempt_id=str(receipt.get("attempt_id") or "") or None,
                        )
                else:
                    response = await self._k._consume(
                        task,
                        state,
                        provider,
                        request,
                        estimator=token_estimator,
                        request_fingerprint=request_fingerprint,
                        attempt_id=str(receipt.get("attempt_id") or "") or None,
                    )
                await self._fault_point("provider-return")
                response_completed_payload: dict[str, Any] = {
                    "provider": selection_for_attempt.provider,
                    "model": selection_for_attempt.model,
                    "role": role,
                    "attempt_index": attempt,
                }
                if inference_kind is not None:
                    response_completed_payload["subturn"] = True
                    response_completed_payload["inference_kind"] = inference_kind
                await self._k._emit("ModelResponseCompleted", response_completed_payload, task)
                state.model_calls += 1
                actual_cost = _actual_model_cost(
                    selection_for_attempt.info,
                    response,
                    request,
                    estimator=token_estimator,
                )
                if actual_cost is None:
                    state.cost_known = False
                if response_store is not None:
                    await response_store.set_actual_usage(
                        attempt_id=str(receipt["attempt_id"]),
                        input_tokens=_input_tokens_of(response, request, estimator=token_estimator),
                        output_tokens=_output_tokens_of(response),
                        cost_usd=actual_cost,
                    )
                if self._k._budgets is not None:
                    # Persist actual usage at the model boundary. The final
                    # TaskResult is an aggregate and BudgetTracker consumes
                    # only any execution/mutation delta during finalization.
                    usage_kwargs: dict[str, Any] = {
                        "input_tokens": _input_tokens_of(
                            response, request, estimator=token_estimator
                        ),
                        "output_tokens": _output_tokens_of(response),
                        "model_calls": 1,
                    }
                    # Reconciliation owns the actual charge when a reservation
                    # was held.  Supplying cost to both paths would double the
                    # charge in the owner ledger.
                    if actual_cost is not None and not reservation:
                        usage_kwargs["cost"] = actual_cost
                    await self._k._budgets.apply_model_accounting(
                        task.id,
                        str(receipt.get("attempt_id") or request.request_id),
                        reserved=worst_cost
                        if reservation and worst_cost is not None
                        else Decimal("0"),
                        input_tokens=int(usage_kwargs["input_tokens"]),
                        output_tokens=int(usage_kwargs["output_tokens"]),
                        actual_cost=actual_cost,
                        reservation_id=str(receipt.get("attempt_id") or request.request_id),
                    )
                    await self._fault_point("budget-charge")
                    await self._fault_point("budget-checkpoint")
                    if response_store is not None:
                        await response_store.mark_budget_accounted(
                            attempt_id=str(receipt["attempt_id"])
                        )
                state.cost += actual_cost or Decimal("0")
                reservation = False
                # Record final usage
                usage_completion_ok = True
                if self._k._provider_usage_store is not None and attempt_usage_id is not None:
                    try:
                        usage = response.usage if response else None
                        await self._k._provider_usage_store.record_completion(
                            attempt_usage_id,
                            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
                            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
                            cost_usd=(str(actual_cost) if actual_cost is not None else None),
                            metadata={
                                "inference": dict(
                                    self._k._inference_metadata(selection_for_attempt)
                                ),
                                "usage": dict(vars(usage)) if usage is not None else {},
                                "role": role,
                                "attempt_index": attempt,
                                "state": "success",
                                "duration_ms": round(
                                    (time.monotonic() - attempt_started) * 1000, 2
                                ),
                            },
                        )
                    except Exception as exc:
                        # P1-11: cost/audit evidence must not vanish silently.
                        usage_completion_ok = False
                        _bookkeeping_failure("provider usage completion record", task, exc)
                    if usage_completion_ok and response_store is not None:
                        await response_store.mark_provider_usage_completed(
                            attempt_id=str(receipt["attempt_id"])
                        )
                    await self._fault_point("usage-completion")
                if response_store is not None and usage_completion_ok:
                    await response_store.mark_accounting_applied(
                        attempt_id=str(receipt["attempt_id"]),
                    )
                return response
            except ProviderError as exc:
                if response_store is not None:
                    try:
                        await response_store.fail(
                            attempt_id=str(receipt["attempt_id"]),
                        )
                    except Exception as receipt_exc:
                        _bookkeeping_failure("model response failure receipt", task, receipt_exc)
                if self._k._budgets is not None and reservation and worst_cost is not None:
                    await self._k._budgets.release_model_cost(
                        task.id,
                        worst_cost,
                        reservation_id=str(receipt.get("attempt_id") or request.request_id),
                    )
                    if response_store is not None:
                        await response_store.mark_reservation_released(
                            attempt_id=str(receipt["attempt_id"])
                        )
                last_err = exc
                state.request_id = None
                if self._k._provider_usage_store is not None and attempt_usage_id is not None:
                    try:
                        await self._k._provider_usage_store.record_completion(
                            attempt_usage_id,
                            input_tokens=0,
                            output_tokens=0,
                            metadata={
                                "inference": dict(
                                    self._k._inference_metadata(selection_for_attempt)
                                ),
                                "role": role,
                                "attempt_index": attempt,
                                "state": "failed",
                                "failure_category": type(exc).__name__,
                                "error_type": type(exc).__name__,
                                "error": str(exc)[:1000],
                                "duration_ms": round(
                                    (time.monotonic() - attempt_started) * 1000, 2
                                ),
                            },
                        )
                    except Exception as record_exc:
                        # P1-11: the failure record IS the audit evidence for
                        # this attempt; losing it silently is worse than the
                        # provider error itself.
                        _bookkeeping_failure("provider usage failure record", task, record_exc)
                if not _is_retryable(exc):
                    raise
                if attempt >= max_attempts - 1:
                    break
                # Exclude the failed (provider, model) pair only; sibling
                # models on the same provider remain candidates.
                attempted.add((selection_for_attempt.provider, selection_for_attempt.model))
                selection_for_attempt = await self._k._select_model(
                    task,
                    compiled_for_attempt,
                    exclude=frozenset(attempted),
                    relax_context=isinstance(exc, ContextOverflow),
                )
                if isinstance(exc, ContextOverflow):
                    fallback_limit = getattr(selection_for_attempt.info, "context_limit", None)
                    current_need = getattr(
                        compiled_for_attempt.requirements, "minimum_context_tokens", None
                    )
                    if (
                        fallback_limit is not None
                        and current_need is not None
                        and fallback_limit < current_need
                    ):
                        compiled_for_attempt = await self._k._compile(
                            task, context_window=int(fallback_limit)
                        )
            except BaseException as exc:
                outcome_unknown = False
                if provider_started and response_store is not None:
                    current = await response_store.get_receipt(
                        task_id=task.id, request_fingerprint=request_fingerprint
                    )
                    if current is not None and str(current.get("status") or "") == "PENDING":
                        outcome_unknown = await response_store.mark_provider_outcome_unknown(
                            attempt_id=str(receipt["attempt_id"])
                        )
                if (
                    self._k._budgets is not None
                    and reservation
                    and worst_cost is not None
                    and not outcome_unknown
                ):
                    await self._k._budgets.release_model_cost(
                        task.id,
                        worst_cost,
                        reservation_id=str(receipt.get("attempt_id") or request.request_id),
                    )
                    if response_store is not None:
                        await response_store.mark_reservation_released(
                            attempt_id=str(receipt["attempt_id"])
                        )
                if outcome_unknown:
                    raise ProviderOutcomeUnknown(
                        "provider outcome became unknown after the request was sent; "
                        f"attempt {receipt.get('attempt_id') or 'unknown'} requires reconciliation"
                    ) from exc
                raise
        raise last_err or ModelUnavailable("no model available")

    # ------------------------------------------------------------------ #
    # Interpreter fusion broker (audit P0.2 / P0.4)
    # ------------------------------------------------------------------ #

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
        """Route one auxiliary inference through this kernel.

        For auxiliary model work that is not a reasoning turn of any task
        (context compression today; embedding/judging helpers later): the
        kernel remains the only component that selects a model, opens a
        provider request, or meters usage. Uses the SAME ModelRouter with
        the caller-named role policy, records usage in the SAME
        provider-usage store, and returns the response text blocks — or
        None on any failure (auxiliary inference is best-effort by
        contract; the caller's deterministic fallback applies).

        Emits no task events; the usage row carries role metadata so
        ``athena inspect`` renders it with its true role.
        """
        if self._k._provider_usage_store is None or self._k._registry is None:
            return None

        usage_id: str | None = None
        budget_id = budget_task_id or task_id
        reservation_amount: Decimal | None = None
        model_lease = None
        try:
            from athena.protocol.messages import Role, TextBlock
            from athena.protocol.tasks import ModelPolicy
            from athena.models.compat.caching import build_cache_key, cache_fingerprint

            selection = await self._k._router.select(
                policy=ModelPolicy(role=role, require_tools=False)
            )
            provider = self._k._registry.provider_for(selection.provider)
            attempt_metadata = dict(metadata or {})
            attempt_metadata.update(
                {
                    "role": role,
                    "purpose": attempt_metadata.get("purpose", "utility_inference"),
                    "state": "started",
                }
            )
            inference_metadata = self._k._inference_metadata(selection)
            namespace = self._k._trusted_cache_namespace()
            stable_payload = (
                [{"role": "system", "block_types": ["text"], "content": system_prompt}]
                if system_prompt
                else []
            )
            attempt_metadata.update(
                {
                    **inference_metadata,
                    "cache_namespace": namespace,
                    "cache_session_key": build_cache_key(
                        namespace=namespace,
                        provider=selection.provider,
                        model=selection.model,
                        profile_fingerprint=str(
                            inference_metadata.get(
                                "provider_profile_fingerprint",
                                inference_metadata.get("provider_profile_id", selection.provider),
                            )
                        ),
                        prefix_fingerprint=cache_fingerprint(stable_payload),
                    ),
                    "cache_prefix_message_count": len(stable_payload),
                }
            )
            usage_id = await self._k._provider_usage_store.record_attempt(
                provider=selection.provider,
                model=selection.model,
                task_id=task_id,
                session_id=session_id,
                metadata=attempt_metadata,
            )
            messages: list[Message] = []
            if system_prompt:
                messages.append(
                    Message(
                        id=new_id("msg"),
                        role=Role.SYSTEM,
                        blocks=(TextBlock(text=system_prompt),),
                        created_at=utcnow(),
                        provenance=Provenance(
                            source_type=SourceType.SYSTEM,
                            trust=TrustClass.CONFIGURED_INSTRUCTION,
                        ),
                    )
                )
            messages.append(
                Message(
                    id=new_id("msg"),
                    role=Role.USER,
                    blocks=(TextBlock(text=user_prompt),),
                    created_at=utcnow(),
                    provenance=Provenance(
                        source_type=SourceType.SYSTEM,
                        trust=TrustClass.CONFIGURED_INSTRUCTION,
                    ),
                )
            )
            request = ModelRequest(
                messages=tuple(messages),
                model=selection.model,
                provider=selection.provider,
                request_id=new_id("sum"),
                metadata=attempt_metadata,
            )
            token_estimator = ModelTokenEstimator.from_profile(
                self._k._registry.model_profile_for(selection.provider, selection.model)
            )
            if self._k._budgets is not None and budget_id:
                remaining = await self._k._budgets.remaining(budget_id)
                worst_cost = _worst_case_cost(selection.info, request, estimator=token_estimator)
                if worst_cost is None and remaining.get("cost_usd") is not None:
                    raise TaskBudgetExceeded(
                        "utility model pricing unknown under hard monetary budget"
                    )
                if (
                    worst_cost is not None
                    and remaining.get("cost_usd") is not None
                    and worst_cost > remaining["cost_usd"]
                ):
                    raise TaskBudgetExceeded(
                        f"utility model call cost {worst_cost} exceeds remaining budget"
                    )
                if worst_cost is not None:
                    await self._k._budgets.reserve_model_cost(budget_id, worst_cost)
                    reservation_amount = worst_cost
                model_lease = self._k._budgets.model_call_lease(budget_id)
                await model_lease.__aenter__()
            parts: list[str] = []
            async with self._k._utility_model_semaphore:
                async for event in provider.complete(request):
                    if getattr(event, "type", None) is not None and event.type.value == "done":
                        resp = event.response
                        if resp is None:
                            continue
                        for block in resp.blocks:
                            if isinstance(block, TextBlock) and block.text:
                                parts.append(block.text)
                        usage = getattr(resp, "usage", None)
                        actual_cost = _actual_model_cost(
                            selection.info,
                            resp,
                            request,
                            estimator=token_estimator,
                        )
                        if self._k._budgets is not None and budget_id:
                            self._k._budgets.consume(
                                budget_id,
                                input_tokens=_input_tokens_of(
                                    resp, request, estimator=token_estimator
                                ),
                                output_tokens=_output_tokens_of(resp),
                                model_calls=1,
                            )
                            if reservation_amount is not None:
                                if actual_cost is None:
                                    await self._k._budgets.release_model_cost(
                                        budget_id, reservation_amount
                                    )
                                else:
                                    await self._k._budgets.reconcile_model_cost(
                                        budget_id,
                                        reserved=reservation_amount,
                                        actual=actual_cost,
                                    )
                                reservation_amount = None
                            persist_budget = getattr(self._k._budgets, "_persist_usage", None)
                            if persist_budget is not None:
                                await persist_budget(budget_id)
                        try:
                            completion_metadata = dict(metadata or {})
                            completion_metadata.update(
                                {
                                    "role": role,
                                    "purpose": completion_metadata.get(
                                        "purpose", "utility_inference"
                                    ),
                                    "state": "success",
                                }
                            )
                            await self._k._provider_usage_store.record_completion(
                                usage_id,
                                input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
                                output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
                                cost_usd=(
                                    str(getattr(usage, "cost_usd"))
                                    if getattr(usage, "cost_usd", None) is not None
                                    else None
                                ),
                                metadata=completion_metadata,
                            )
                            usage_id = None
                        except Exception as exc:
                            # P1-11: usage evidence must not vanish silently.
                            _bookkeeping_failure("utility usage completion record", task_id, exc)
            if model_lease is not None:
                await model_lease.__aexit__(None, None, None)
                model_lease = None
            if reservation_amount is not None and budget_id and self._k._budgets is not None:
                await self._k._budgets.release_model_cost(budget_id, reservation_amount)
                reservation_amount = None
            if usage_id is not None:
                # started but never completed (stream ended without done);
                # keep role metadata — record_completion REPLACES it
                try:
                    failure_metadata = dict(metadata or {})
                    failure_metadata.update(
                        {"role": role, "purpose": "utility_inference", "state": "no_done_event"}
                    )
                    await self._k._provider_usage_store.record_completion(
                        usage_id,
                        input_tokens=0,
                        output_tokens=0,
                        metadata=failure_metadata,
                    )
                except Exception as exc:
                    # P1-11: this row is the only evidence the call happened.
                    _bookkeeping_failure("utility usage no-done closure", task_id, exc)
            return " ".join(parts).strip() or None
        except Exception:
            if model_lease is not None:
                try:
                    await model_lease.__aexit__(None, None, None)
                except Exception as exc:
                    _logger.debug(
                        "utility inference lease release failed (task=%s): %s",
                        task_id or "?",
                        exc,
                    )
                model_lease = None
            if reservation_amount is not None and budget_id and self._k._budgets is not None:
                try:
                    await self._k._budgets.release_model_cost(budget_id, reservation_amount)
                except Exception as exc:
                    _bookkeeping_failure("utility budget reservation release", task_id, exc)
                reservation_amount = None
            if usage_id is not None:
                # started but never completed (exception mid-stream): close
                # the row honestly rather than leaving it in-flight forever
                try:
                    failure_metadata = dict(metadata or {})
                    failure_metadata.update(
                        {"role": role, "purpose": "utility_inference", "state": "error"}
                    )
                    await self._k._provider_usage_store.record_completion(
                        usage_id,
                        input_tokens=0,
                        output_tokens=0,
                        metadata=failure_metadata,
                    )
                except Exception as exc:
                    # P1-11: this row is the only evidence the call happened.
                    _bookkeeping_failure("utility usage error closure", task_id, exc)
            _logger.debug("utility_inference failed; deterministic fallback", exc_info=True)
            return None

    def _inference_metadata(self, selection: ModelSelection) -> dict[str, Any]:
        profile = self._k._registry.profile_for(selection.provider)
        if profile is None:
            return {"provider_profile_id": selection.provider}
        fingerprint = getattr(profile, "fingerprint", None)
        profile_fingerprint = (
            fingerprint()
            if callable(fingerprint)
            else str(getattr(profile, "id", selection.provider))
        )
        profile_id = str(getattr(profile, "id", selection.provider))
        model_profile = self._k._registry.model_profile_for(selection.provider, selection.model)
        from athena.models.compat.profiles import resolve_compatibility_profile

        compatibility = resolve_compatibility_profile(
            str(getattr(profile, "compatibility_profile", "auto"))
        )
        return {
            # ID is the stable configured route identity. The fingerprint is
            # separate because changing a route's wire semantics must still
            # create an explicit cache/replay boundary for the same ID.
            "provider_profile_id": profile_id,
            "provider_profile_fingerprint": profile_fingerprint,
            "profile_id": getattr(profile, "id", selection.provider),
            "cache_mode": getattr(profile, "cache_mode", "none"),
            "cache_session_key": (
                f"{selection.provider}:{selection.model}"
                if getattr(profile, "cache_session_key", False)
                else None
            ),
            "compatibility_profile": getattr(profile, "compatibility_profile", "auto"),
            "tool_repair_mode": compatibility.tool_repair,
            "max_tool_correction_cycles": compatibility.max_tool_correction_cycles,
            "protocol": getattr(profile, "protocol", "openai-compat"),
            "idempotency_semantics": getattr(profile, "idempotency_semantics", "none"),
            "model_profile": (dict(vars(model_profile)) if model_profile is not None else None),
        }

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
        """Observe the rendered prefix and derive its cache partition key."""
        from athena.models.compat.caching import (
            PrefixTracker,
            PromptEnvelope,
            build_cache_key,
            cache_message_payload,
        )

        session_key = task.session_id or task.id
        namespace = self._k._trusted_cache_namespace(task)
        metadata = self._k._inference_metadata(selection)
        # Keep the tracker stable across profile revisions so it can emit a
        # provider-profile boundary instead of silently starting a new tracker.
        key = (namespace, session_key, selection.provider)
        profile_id = str(metadata.get("provider_profile_id", selection.provider))
        tracker = self._k._prefix_trackers.setdefault(key, PrefixTracker())
        if tracker.last_prefix_fp is None and self._k._events is not None:
            try:
                latest = getattr(self._k._events, "latest_for_session", None)
                event = (
                    await latest(session_key, "InferencePrefixObserved")
                    if callable(latest)
                    else None
                )
                if event is not None and (
                    event.payload.get("cache_namespace") in {None, namespace}
                ):
                    payload = dict(event.payload or {})
                    tracker.last_prefix_fp = payload.get("prefix_fingerprint")
                    tracker.last_full_fp = payload.get("full_fingerprint")
                    tracker.components_fp = dict(payload.get("components_fp") or {})
                else:
                    # Compatibility with older event-store adapters.
                    for event in reversed(await self._k._events.list_for_session(session_key)):
                        if event.type != "InferencePrefixObserved":
                            continue
                        payload = dict(event.payload or {})
                        if payload.get("cache_namespace") not in {None, namespace}:
                            continue
                        tracker.last_prefix_fp = payload.get("prefix_fingerprint")
                        tracker.last_full_fp = payload.get("full_fingerprint")
                        tracker.components_fp = dict(payload.get("components_fp") or {})
                        break
            except Exception as exc:
                _logger.debug("prefix tracker restore failed for %s: %s", session_key, exc)
        stable_messages = tuple(getattr(compiled, "cache_prefix_messages", ()) or ())
        stable_payload = [cache_message_payload(message) for message in stable_messages]
        dynamic_messages = compiled.messages[len(stable_messages) :]
        tools_payload = [
            {
                "name": descriptor.id,
                "description": descriptor.description or f"Athena capability {descriptor.id}",
                "parameters": descriptor.input_schema or {"type": "object", "properties": {}},
            }
            for descriptor in compiled.capability_definitions
        ]
        envelope = PromptEnvelope(
            stable_prefix=[stable_payload, tools_payload],
            append_history=[m.id for m in dynamic_messages],
            dynamic_suffix=[cache_message_payload(m) for m in dynamic_messages],
        )
        observed = tracker.observe(
            envelope,
            components={
                "stable_context": stable_payload,
                "tools": tools_payload,
                "model": selection.model,
                "provider_profile": metadata.get("provider_profile_fingerprint", profile_id),
                "compatibility_policy": {
                    "profile": metadata.get("compatibility_profile", "auto"),
                    "repair": metadata.get("tool_repair_mode", "safe"),
                    "correction_cycles": metadata.get("max_tool_correction_cycles", 0),
                    "protocol": metadata.get("protocol", "openai-compat"),
                },
            },
        )
        cache_key = build_cache_key(
            namespace=namespace,
            provider=selection.provider,
            model=selection.model,
            profile_fingerprint=str(metadata.get("provider_profile_fingerprint", profile_id)),
            prefix_fingerprint=observed["prefix_fp"],
        )
        output = {
            "prefix_fingerprint": observed["prefix_fp"],
            "full_fingerprint": tracker.last_full_fp,
            "components_fp": dict(tracker.components_fp),
            "boundary": observed.get("boundary"),
            "cache_boundary": observed.get("boundary"),
            "cache_session_key": cache_key,
            "cache_namespace": namespace,
            "cache_prefix_message_count": len(stable_messages),
        }
        await self._k._emit("InferencePrefixObserved", output, task)
        return output

    def _replay_metadata(
        self,
        compiled: CompiledContext,
        selection: ModelSelection,
    ) -> dict[str, Any]:
        """Reload durable assistant-turn receipts for a resumed request.

        Canonical messages remain the source of truth for provider replay. The
        receipts are carried as request metadata for adapters/inspection and
        are compared against the selected route so a provider/model switch is
        an explicit replay boundary rather than an accidental continuation.
        """
        receipts: list[dict[str, Any]] = []
        for message in compiled.messages:
            value = (message.metadata or {}).get("inference_receipt")
            if isinstance(value, dict):
                receipts.append(dict(value))
        if not receipts:
            return {"replay_receipts": (), "replay_compatible": True}
        last = receipts[-1]
        current_profile = str(
            self._k._inference_metadata(selection).get("provider_profile_id", selection.provider)
        )
        last_profile = str(last.get("provider_profile_id") or "")
        last_model = str(last.get("model_id") or "")
        boundary = None
        if last_profile and last_profile != current_profile:
            boundary = {
                "reason": "provider_profile_changed",
                "from": last_profile,
                "to": current_profile,
            }
        elif last_model and last_model != selection.model:
            boundary = {
                "reason": "model_changed",
                "from": last_model,
                "to": selection.model,
            }
        return {
            "replay_receipts": tuple(receipts[-8:]),
            "replay_compatible": boundary is None,
            "replay_boundary": boundary,
            "boundary": boundary,
        }

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
        accumulator = ModelResponseAccumulator(request)

        async def consume_stream() -> None:
            async for event in provider.complete(request):
                if state.cancel.is_set():
                    raise RequestCancelled("task cancelled")
                accumulator.ingest(event)
                if event.type.value == "delta" and event.delta is not None:
                    await self._k._relay_delta(task, event.delta)
                elif event.type.value == "reasoning" and event.delta is not None:
                    await self._k._emit("ModelReasoningDelta", {}, task)
                    if self._k._model_sink is not None and event.delta.reasoning:
                        await self._k._maybe_await(self._k._model_sink(event.delta.reasoning))
                elif event.type.value == "failed":
                    raise ProviderError(event.error or "provider failed", code=event.code)

        remaining = self._k._remaining_runtime_seconds(task, state)
        if remaining is not None and remaining <= 0:
            raise TaskDeadlineExceeded("task runtime budget exhausted before provider call")
        try:
            if remaining is None:
                await consume_stream()
            else:
                async with asyncio.timeout(remaining):
                    await consume_stream()
        except TimeoutError as exc:
            try:
                await provider.cancel(request.request_id)
            except Exception:
                _logger.debug("provider cancellation after deadline failed", exc_info=True)
            raise TaskDeadlineExceeded("task deadline or wall-time budget exceeded") from exc

        # The accumulator is the only owner of final mixed-content assembly.
        final = accumulator.finish()
        # Providers own wire translation, but the request owns the canonical
        # inference identity. Carry it onto the response before the assistant
        # turn is persisted so durable receipts never fall back to a bare
        # provider name.
        response_metadata = dict(final.metadata)
        for key in (
            "task_id",
            "session_id",
            "provider_profile_id",
            "provider_profile_fingerprint",
            "profile_id",
            "model_id",
            "compatibility_profile",
            "model_profile",
            "protocol",
            "idempotency_semantics",
            "tool_repair_mode",
            "max_tool_correction_cycles",
            "cache_mode",
            "cache_session_key",
            "cache_namespace",
            "cache_prefix_message_count",
            "prefix_fingerprint",
            "full_fingerprint",
            "components_fp",
        ):
            if key in request.metadata and key not in response_metadata:
                response_metadata[key] = request.metadata[key]
        response_metadata["request_id"] = request.request_id
        if attempt_id:
            response_metadata["inference_attempt_id"] = attempt_id
        from athena.models.compat.caching import UsageRecord

        usage_metadata = dict(final.usage.provider_metadata or {})
        raw_usage = usage_metadata.get("raw_usage")
        if isinstance(raw_usage, dict):
            if request.metadata.get("protocol") == "anthropic":
                normalized = UsageRecord.from_anthropic(raw_usage)
            else:
                normalized = UsageRecord.from_openai_compat(raw_usage)
        else:
            normalized = UsageRecord(
                prompt_tokens=final.usage.input_tokens,
                completion_tokens=final.usage.output_tokens,
                cache_read_tokens=final.usage.cache_read_tokens,
                cache_write_tokens=final.usage.cache_write_tokens,
                uncached_prompt_tokens=final.usage.uncached_input_tokens,
            )
        response_metadata["usage_record"] = normalized.to_dict()
        usage = replace(
            final.usage,
            provider_metadata={**usage_metadata, "normalized": normalized.to_dict()},
        )
        final = replace(final, usage=usage, metadata=response_metadata)
        response_store = getattr(self._k, "_model_response_store", None)
        if response_store is not None and request_fingerprint is not None:
            # This is the last local point before the durable response commit.
            # A crash/fault here means the provider outcome may be real but is
            # not locally observable; the caller must record UNKNOWN and must
            # not silently retry a non-idempotent request.
            await self._fault_point("provider-assembled-before-receipt")
            await response_store.complete(
                attempt_id=attempt_id or "",
                response=final,
                provider_response_id=str(final.metadata.get("response_id") or "") or None,
            )
            await self._fault_point("response-receipt-commit")
        state.input_tokens += _input_tokens_of(final, request, estimator=estimator)
        state.output_tokens += _output_tokens_of(final)
        return final

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
