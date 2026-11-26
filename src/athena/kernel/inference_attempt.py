"""One provider-attempt state transition for :mod:`inference_broker`.

This module owns the mechanics of reserving/charging one provider attempt and
recording its failure evidence.  Model selection and retry choice remain in
``InferenceBroker``; this helper never selects a model or starts a second
inference loop.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Mapping

from athena.kernel.tokens import bookkeeping_failure as _bookkeeping_failure
from athena.kernel.inference_accounting import account_successful_attempt
from athena.protocol.errors import ProviderError, ProviderOutcomeUnknown


async def run_provider_attempt(
    broker: Any,
    task: Any,
    state: Any,
    provider: Any,
    request: Any,
    *,
    estimator: Any,
    request_fingerprint: str,
    receipt: Mapping[str, Any],
    selection: Any,
    reservation: bool,
    worst_cost: Decimal | None,
    attempt_usage_id: str | None,
    attempt_started: float,
    role: str,
    attempt: int,
    inference_kind: str | None,
) -> tuple[Any, Decimal | None]:
    """Invoke one provider and reconcile its durable attempt record."""
    response_store = getattr(broker._k, "_model_response_store", None)
    provider_started = False
    try:
        provider_started = True
        if broker._k._budgets is not None:
            async with broker._k._budgets.model_call_lease(task.id):
                response = await broker._k._consume(
                    task,
                    state,
                    provider,
                    request,
                    estimator=estimator,
                    request_fingerprint=request_fingerprint,
                    attempt_id=str(receipt.get("attempt_id") or "") or None,
                )
        else:
            response = await broker._k._consume(
                task,
                state,
                provider,
                request,
                estimator=estimator,
                request_fingerprint=request_fingerprint,
                attempt_id=str(receipt.get("attempt_id") or "") or None,
            )
        await broker._fault_point("provider-return")
        payload: dict[str, Any] = {
            "provider": selection.provider,
            "model": selection.model,
            "role": role,
            "attempt_index": attempt,
        }
        if inference_kind is not None:
            payload["subturn"] = True
            payload["inference_kind"] = inference_kind
        await broker._k._emit("ModelResponseCompleted", payload, task)
        actual_cost = await account_successful_attempt(
            broker,
            task,
            request=request,
            response=response,
            receipt=receipt,
            selection=selection,
            estimator=estimator,
            reservation=reservation,
            worst_cost=worst_cost,
            attempt_usage_id=attempt_usage_id,
            attempt_started=attempt_started,
            role=role,
        )
        return response, actual_cost
    except ProviderError as exc:
        if response_store is not None:
            try:
                await response_store.fail(attempt_id=str(receipt["attempt_id"]))
            except Exception as receipt_exc:  # rationale: retain failure receipt evidence
                _bookkeeping_failure("model response failure receipt", task, receipt_exc)
        if broker._k._budgets is not None and reservation and worst_cost is not None:
            await broker._k._budgets.release_model_cost(
                task.id,
                worst_cost,
                reservation_id=str(receipt.get("attempt_id") or request.request_id),
            )
            if response_store is not None:
                await response_store.mark_reservation_released(
                    attempt_id=str(receipt["attempt_id"])
                )
        state.request_id = None
        if broker._k._provider_usage_store is not None and attempt_usage_id is not None:
            try:
                await broker._k._provider_usage_store.record_completion(
                    attempt_usage_id,
                    input_tokens=0,
                    output_tokens=0,
                    metadata={
                        "inference": dict(broker._k._inference_metadata(selection)),
                        "role": role,
                        "attempt_index": attempt,
                        "state": "failed",
                        "failure_category": type(exc).__name__,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:1000],
                        "duration_ms": round((time.monotonic() - attempt_started) * 1000, 2),
                    },
                )
            except Exception as record_exc:  # rationale: retain provider failure evidence
                _bookkeeping_failure("provider usage failure record", task, record_exc)
        raise
    except BaseException as exc:  # rationale: reconcile uncertain provider outcomes
        outcome_unknown = False
        if provider_started and response_store is not None:
            current = await response_store.get_receipt(
                task_id=task.id, request_fingerprint=request_fingerprint
            )
            if current is not None and (
                str(current.get("status") or "") in {"PENDING", "ACCOUNTED"}
                or (
                    str(current.get("status") or "") == "COMPLETED"
                    and current.get("accounting_applied_at") is None
                )
            ):
                outcome_unknown = await response_store.mark_provider_outcome_unknown(
                    attempt_id=str(receipt["attempt_id"])
                )
        if (
            broker._k._budgets is not None
            and reservation
            and worst_cost is not None
            and not outcome_unknown
        ):
            await broker._k._budgets.release_model_cost(
                task.id,
                worst_cost,
                reservation_id=str(receipt.get("attempt_id") or request.request_id),
            )
            if response_store is not None:
                await response_store.mark_reservation_released(
                    attempt_id=str(receipt["attempt_id"])
                )
        if outcome_unknown:
            if isinstance(exc, ProviderOutcomeUnknown):
                raise
            raise ProviderOutcomeUnknown(
                "provider outcome became unknown after the request was sent; "
                f"attempt {receipt.get('attempt_id') or 'unknown'} requires reconciliation"
            ) from exc
        raise


__all__ = ["run_provider_attempt"]
