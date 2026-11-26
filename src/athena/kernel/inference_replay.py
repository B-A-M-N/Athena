"""Durable cached-response replay for the inference broker."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any, Mapping

from athena.models.router import ModelSelection
from athena.models.tokens import ModelTokenEstimator
from athena.protocol.models import ModelRequest, ModelResponse
from athena.protocol.tasks import TaskSpec
from athena.kernel.tokens import (
    actual_model_cost as _actual_model_cost,
    input_tokens_of as _input_tokens_of,
    output_tokens_of as _output_tokens_of,
)

if TYPE_CHECKING:
    from athena.kernel.kernel import RunState


async def replay_cached_attempt(
    broker: Any,
    task: TaskSpec,
    state: RunState,
    *,
    receipt: Mapping[str, Any],
    request: ModelRequest,
    request_fingerprint: str,
    response: ModelResponse,
    estimator: ModelTokenEstimator,
    selection: ModelSelection,
    attempt: int,
) -> ModelResponse:
    """Reconcile a durable provider response without redispatching it."""
    await broker._reconcile_receipt(
        task,
        {**receipt, "request_fingerprint": request_fingerprint},
        response=response,
        request=request,
        estimator=estimator,
        selection=selection,
    )
    state.request_id = response.request_id
    state.provider = response.provider
    state.model_calls += 1
    state.input_tokens += _input_tokens_of(response, request, estimator=estimator)
    state.output_tokens += _output_tokens_of(response)
    cached_cost = _actual_model_cost(selection.info, response, request, estimator=estimator)
    if cached_cost is None:
        state.cost_known = False
    state.cost += cached_cost or Decimal("0")
    await broker._k._emit(
        "ModelResponseReplayed",
        {
            "provider": response.provider,
            "model": response.model,
            "request_id": response.request_id,
            "attempt_index": attempt,
        },
        task,
    )
    return response


__all__ = ["replay_cached_attempt"]
