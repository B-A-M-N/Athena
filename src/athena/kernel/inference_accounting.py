"""Provider-attempt usage and budget reconciliation."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from athena.kernel.tokens import (
    actual_model_cost as _actual_model_cost,
    bookkeeping_failure as _bookkeeping_failure,
    input_tokens_of as _input_tokens_of,
    output_tokens_of as _output_tokens_of,
)

__all__ = ["account_successful_attempt"]


async def account_successful_attempt(
    broker: Any,
    task: Any,
    *,
    request: Any,
    response: Any,
    receipt: Any,
    selection: Any,
    estimator: Any,
    reservation: bool,
    worst_cost: Decimal | None,
    attempt_usage_id: str | None,
    attempt_started: float,
    role: str,
) -> Decimal | None:
    """Persist usage and budget accounting for one successful provider call."""
    actual_cost = _actual_model_cost(selection.info, response, request, estimator=estimator)
    response_store = getattr(broker._k, "_model_response_store", None)
    if response_store is not None:
        await response_store.set_actual_usage(
            attempt_id=str(receipt["attempt_id"]),
            input_tokens=_input_tokens_of(response, request, estimator=estimator),
            output_tokens=_output_tokens_of(response),
            cost_usd=actual_cost,
        )
    if broker._k._budgets is not None:
        usage_cost = actual_cost if actual_cost is not None and not reservation else None
        await broker._k._budgets.apply_model_accounting(
            task.id,
            str(receipt.get("attempt_id") or request.request_id),
            reserved=worst_cost if reservation and worst_cost is not None else Decimal("0"),
            input_tokens=_input_tokens_of(response, request, estimator=estimator),
            output_tokens=_output_tokens_of(response),
            actual_cost=usage_cost,
            reservation_id=str(receipt.get("attempt_id") or request.request_id),
        )
        await broker._fault_point("budget-charge")
        await broker._fault_point("budget-checkpoint")
        if response_store is not None:
            await response_store.mark_budget_accounted(attempt_id=str(receipt["attempt_id"]))
    if broker._k._provider_usage_store is not None and attempt_usage_id is not None:
        try:
            usage = response.usage
            await broker._k._provider_usage_store.record_completion(
                attempt_usage_id,
                input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
                output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
                cost_usd=(str(actual_cost) if actual_cost is not None else None),
                metadata={
                    "inference": dict(broker._inference_metadata(selection)),
                    "usage": dict(vars(usage)) if usage is not None else {},
                    "role": role,
                    "state": "success",
                    "duration_ms": round((time.monotonic() - attempt_started) * 1000, 2),
                },
            )
        except Exception as exc:  # rationale: retain provider usage audit evidence
            _bookkeeping_failure("provider usage completion record", task, exc)
        if response_store is not None:
            await response_store.mark_provider_usage_completed(
                attempt_id=str(receipt["attempt_id"])
            )
        await broker._fault_point("usage-completion")
    if response_store is not None:
        await response_store.mark_accounting_applied(attempt_id=str(receipt["attempt_id"]))
    return actual_cost
