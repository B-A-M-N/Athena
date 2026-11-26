"""Provider-stream collection for the registry's direct invocation API."""

from __future__ import annotations

from typing import Any

from athena.protocol.errors import ModelUnavailable, ProviderError, ProviderOutcomeUnknown
from athena.protocol.models import (
    IncompleteModelResponse,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelResponseAccumulator,
    StreamOutputLimitExceeded,
)


async def collect_response(provider: ModelProvider, request: ModelRequest) -> ModelResponse:
    """Accumulate one provider stream with cancellation-safe local limits."""
    accumulator = ModelResponseAccumulator(request)
    try:
        async for event in provider.complete(request):
            accumulator.ingest(event)
            if event.type.value == "failed":
                data: dict[str, Any] = {}
                if accumulator.has_partial_output:
                    data["partial_output"] = accumulator.diagnostic()
                raise ProviderError(event.error or "provider failed", code=event.code, **data)
    except StreamOutputLimitExceeded as exc:
        cancel = getattr(provider, "cancel", None)
        if callable(cancel):
            try:
                await cancel(request.request_id)
            except Exception:
                pass
        raise ProviderOutcomeUnknown(
            "provider stream exceeded the local output limit",
            partial_output=accumulator.diagnostic(),
            estimated_output_tokens=exc.estimated_tokens,
            output_bytes=exc.raw_bytes,
            output_token_limit=exc.token_limit,
            output_byte_limit=exc.byte_limit,
        ) from exc
    try:
        return accumulator.finish()
    except IncompleteModelResponse as exc:
        if accumulator.has_partial_output:
            raise ProviderOutcomeUnknown(
                "provider stream ended after partial output without a terminal response",
                partial_output=exc.diagnostic,
            ) from exc
        raise ModelUnavailable(
            f"provider produced no terminal response for {request.request_id}"
        ) from exc


__all__ = ["collect_response"]
