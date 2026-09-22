"""Provider stream consumption for :mod:`inference_broker`.

This module owns the mechanics after a provider has been selected: cancellation
and deadline handling, mixed-content assembly, usage normalization, and the
durable response-receipt commit.  Selection, retry policy, and accounting
authority remain with ``InferenceBroker`` and the owning ``AgentKernel``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

from athena.kernel.tokens import input_tokens_of as _input_tokens_of
from athena.kernel.tokens import output_tokens_of as _output_tokens_of
from athena.models.tokens import ModelTokenEstimator
from athena.protocol.errors import (
    ModelUnavailable,
    ProviderError,
    ProviderOutcomeUnknown,
    RequestCancelled,
    TaskDeadlineExceeded,
)
from athena.protocol.models import (
    IncompleteModelResponse,
    ModelRequest,
    ModelResponse,
    ModelResponseAccumulator,
    StreamOutputLimitExceeded,
)

__all__ = ["consume_provider_stream"]

_logger = logging.getLogger("athena.kernel")


async def consume_provider_stream(
    broker: Any,
    task: Any,
    state: Any,
    provider: Any,
    request: ModelRequest,
    *,
    estimator: ModelTokenEstimator | None = None,
    request_fingerprint: str | None = None,
    attempt_id: str | None = None,
) -> ModelResponse:
    """Consume one selected provider stream and commit its local outcome."""
    kernel = broker._k
    accumulator = ModelResponseAccumulator(request)

    async def consume_stream() -> None:
        async for event in provider.complete(request):
            if state.cancel.is_set():
                raise RequestCancelled("task cancelled")
            accumulator.ingest(event)
            if event.type.value == "delta" and event.delta is not None:
                await broker._relay_delta(task, event.delta)
            elif event.type.value == "reasoning" and event.delta is not None:
                await kernel._emit("ModelReasoningDelta", {}, task)
                if kernel._model_sink is not None and event.delta.reasoning:
                    await kernel._maybe_await(kernel._model_sink(event.delta.reasoning))
            elif event.type.value == "failed":
                data: dict[str, Any] = {}
                if accumulator.has_partial_output:
                    data["partial_output"] = accumulator.diagnostic()
                raise ProviderError(
                    event.error or "provider failed",
                    code=event.code,
                    **data,
                )

    remaining = kernel._remaining_runtime_seconds(task, state)
    if remaining is not None and remaining <= 0:
        raise TaskDeadlineExceeded("task runtime budget exhausted before provider call")
    try:
        if remaining is None:
            await consume_stream()
        else:
            async with asyncio.timeout(remaining):
                await consume_stream()
    except StreamOutputLimitExceeded as exc:
        try:
            await provider.cancel(request.request_id)
        except Exception:  # rationale: local limit truth must not be masked by cleanup
            _logger.debug("provider cancellation after output limit failed", exc_info=True)
        raise ProviderOutcomeUnknown(
            "provider stream exceeded the local output limit",
            partial_output=accumulator.diagnostic(),
            estimated_output_tokens=exc.estimated_tokens,
            output_bytes=exc.raw_bytes,
            output_token_limit=exc.token_limit,
            output_byte_limit=exc.byte_limit,
        ) from exc
    except TimeoutError as exc:
        try:
            await provider.cancel(request.request_id)
        except Exception:  # rationale: deadline cleanup must not mask timeout truth
            _logger.debug("provider cancellation after deadline failed", exc_info=True)
        raise TaskDeadlineExceeded("task deadline or wall-time budget exceeded") from exc

    # The accumulator is the only owner of final mixed-content assembly.
    try:
        final = accumulator.finish()
    except IncompleteModelResponse as exc:
        if accumulator.has_partial_output:
            raise ProviderOutcomeUnknown(
                "provider stream ended after partial output without a terminal response",
                partial_output=exc.diagnostic,
            ) from exc
        raise ModelUnavailable("provider stream ended without a terminal response") from exc
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
    response_store = getattr(kernel, "_model_response_store", None)
    if response_store is not None and request_fingerprint is not None:
        # This is the last local point before the durable response commit.
        # A crash/fault here means the provider outcome may be real but is
        # not locally observable; the caller must record UNKNOWN and must
        # not silently retry a non-idempotent request.
        await broker._fault_point("provider-assembled-before-receipt")
        await response_store.complete(
            attempt_id=attempt_id or "",
            response=final,
            provider_response_id=str(final.metadata.get("response_id") or "") or None,
        )
        await broker._fault_point("response-receipt-commit")
    state.input_tokens += _input_tokens_of(final, request, estimator=estimator)
    state.output_tokens += _output_tokens_of(final)
    return final
