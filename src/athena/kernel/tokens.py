"""Pure token accounting and cost helpers for the inference kernel.

Owned by neither the kernel nor the broker: these are pure functions on
protocol/model types. Extracted from kernel.py so inference_broker.py no
longer needs a dynamic owner-backimport (review item 15).
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from athena.models.tokens import ModelTokenEstimator
from athena.protocol.models import ModelRequest, ModelResponse

if TYPE_CHECKING:
    pass

__all__ = [
    "estimate_input_tokens",
    "display_input_estimate",
    "input_tokens_of",
    "output_tokens_of",
    "actual_model_cost",
    "worst_case_cost",
    "is_retryable",
    "bookkeeping_failure",
]


def estimate_input_tokens(
    request: ModelRequest,
    *,
    estimator: ModelTokenEstimator | None = None,
) -> int | None:
    """Return the hard-admission bound for the complete request envelope."""
    return (estimator or ModelTokenEstimator()).upper_bound(request)


def display_input_estimate(request: ModelRequest) -> int:
    """Cheap fallback for usage display when a profile declares no bound."""
    text = (
        (request.system or "")
        + "\n"
        + "\n".join(
            (
                message.conversation_text()
                if callable(getattr(message, "conversation_text", None))
                else message.text()
            )
            or ""
            for message in request.messages
        )
    )
    return max(1, (len(text) + 3) // 4)


def input_tokens_of(
    response: ModelResponse,
    request: ModelRequest,
    *,
    estimator: ModelTokenEstimator | None = None,
) -> int:
    """Return reported input tokens or a conservative fallback ledger value."""
    try:
        usage = response.usage
    except AttributeError:
        usage = None
    count = int(getattr(usage, "input_tokens", None) or 0)
    if count > 0:
        return count
    estimate = estimate_input_tokens(request, estimator=estimator)
    return estimate if estimate is not None else display_input_estimate(request)


def output_tokens_of(response: ModelResponse) -> int:
    """Return real output-token count when reported; else a chars/4 estimate."""
    try:
        usage = response.usage
    except AttributeError:
        usage = None
    count = int(getattr(usage, "output_tokens", None) or 0)
    if count > 0:
        return count
    return sum(len(getattr(b, "text", None) or "") for b in response.blocks) // 4


def actual_model_cost(
    info,
    response: ModelResponse,
    request: ModelRequest,
    *,
    estimator: ModelTokenEstimator | None = None,
) -> Decimal | None:
    """Prefer provider cost, then declared model pricing, else unknown."""
    usage = getattr(response, "usage", None)
    reported = getattr(usage, "cost_usd", None)
    if reported is not None:
        try:
            return Decimal(str(reported))
        except (TypeError, ValueError):
            pass
    pricing = getattr(info, "cost", None)
    if pricing is None:
        return None
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0) or estimate_input_tokens(
        request, estimator=estimator
    )
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0) or output_tokens_of(response)
    if input_tokens is None:
        return None
    cache_read = int(getattr(usage, "cache_read_tokens", 0) or 0)
    cache_write = int(getattr(usage, "cache_write_tokens", 0) or 0)
    uncached = getattr(usage, "uncached_input_tokens", None)
    if uncached is None:
        uncached = max(input_tokens - cache_read - cache_write, 0)
    else:
        uncached = max(int(uncached), 0)
    if pricing.per_1m_input is None and uncached:
        return None
    if cache_read and pricing.per_1m_cache_read_input is None:
        return None
    if cache_write and pricing.per_1m_cache_write_input is None:
        return None
    if pricing.per_1m_output is None and output_tokens:
        return None
    return (
        Decimal(str(pricing.per_1m_input or 0)) * uncached
        + Decimal(str(pricing.per_1m_cache_read_input or 0)) * cache_read
        + Decimal(str(pricing.per_1m_cache_write_input or 0)) * cache_write
        + Decimal(str(pricing.per_1m_output or 0)) * output_tokens
    ) / Decimal(1_000_000)


def worst_case_cost(
    info,
    request: ModelRequest,
    *,
    estimator: ModelTokenEstimator | None = None,
) -> Decimal | None:
    """Estimate the bounded maximum cost from the actual compiled request."""
    input_tokens = estimate_input_tokens(request, estimator=estimator)
    output_tokens = request.max_tokens or getattr(info, "max_output_tokens", None) or 4096
    pricing = getattr(info, "cost", None)
    if pricing is None or input_tokens is None:
        return None
    if pricing.per_1m_input is None or pricing.per_1m_output is None:
        return None
    cache_mode = str((request.metadata or {}).get("cache_mode") or "none").strip().lower()
    input_rates = [Decimal(str(pricing.per_1m_input))]
    if cache_mode not in {"", "none", "off"}:
        if pricing.per_1m_cache_read_input is None or pricing.per_1m_cache_write_input is None:
            return None
        input_rates.extend(
            [
                Decimal(str(pricing.per_1m_cache_read_input)),
                Decimal(str(pricing.per_1m_cache_write_input)),
            ]
        )
    input_rate = max(input_rates)
    output_rate = Decimal(str(pricing.per_1m_output))
    return (input_rate * input_tokens + output_rate * output_tokens) / Decimal(1_000_000)


def is_retryable(exc) -> bool:
    return bool(getattr(exc, "retryable", False))


def bookkeeping_failure(what: str, task, exc: BaseException) -> None:
    """Import-late to avoid cycle; delegate to kernel's logger-based helper."""
    from athena.kernel.kernel import _bookkeeping_failure as _impl

    _impl(what, task, exc)
