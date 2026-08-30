from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from athena.kernel.kernel import _actual_model_cost, _input_tokens_of, _worst_case_cost
from athena.models.tokens import ModelTokenEstimator
from athena.protocol.messages import TextBlock
from athena.protocol.models import CostInfo, ModelRequest, ModelResponse, UsageInfo


def _request(*, cache_mode: str = "none", max_tokens: int = 100) -> ModelRequest:
    return ModelRequest(
        messages=(),
        model="model-a",
        provider="provider-a",
        request_id="request-a",
        max_tokens=max_tokens,
        metadata={"cache_mode": cache_mode},
    )


def _info(cost: CostInfo):
    return SimpleNamespace(cost=cost, max_output_tokens=100)


def test_input_ledger_counts_anthropic_cache_subdivisions():
    response = ModelResponse(
        request_id="request-a",
        model="model-a",
        provider="provider-a",
        usage=UsageInfo(
            input_tokens=1050,
            uncached_input_tokens=100,
            cache_read_tokens=900,
            cache_write_tokens=50,
        ),
    )

    assert _input_tokens_of(response, _request()) == 1050


def test_actual_cost_uses_cache_specific_rates_and_preserves_provider_cost():
    info = _info(
        CostInfo(
            per_1m_input=1.0,
            per_1m_output=2.0,
            per_1m_cache_read_input=0.1,
            per_1m_cache_write_input=0.5,
        )
    )
    response = ModelResponse(
        request_id="request-a",
        model="model-a",
        provider="provider-a",
        blocks=(TextBlock(text="done"),),
        usage=UsageInfo(
            input_tokens=1050,
            output_tokens=20,
            uncached_input_tokens=100,
            cache_read_tokens=900,
            cache_write_tokens=50,
        ),
    )

    assert _actual_model_cost(info, response, _request()) == Decimal("0.000255")

    reported = ModelResponse(
        request_id=response.request_id,
        model=response.model,
        provider=response.provider,
        usage=UsageInfo(cost_usd=0.000123, input_tokens=1050, output_tokens=20),
    )
    assert _actual_model_cost(info, reported, _request()) == Decimal("0.000123")


def test_unknown_cache_rates_make_actual_cost_unknown():
    info = _info(CostInfo(per_1m_input=1.0, per_1m_output=2.0))
    response = ModelResponse(
        request_id="request-a",
        model="model-a",
        provider="provider-a",
        usage=UsageInfo(input_tokens=1000, cache_read_tokens=1000),
    )

    assert _actual_model_cost(info, response, _request()) is None


def test_worst_case_cost_uses_maximum_active_cache_rate():
    info = _info(
        CostInfo(
            per_1m_input=1.0,
            per_1m_output=2.0,
            per_1m_cache_read_input=0.1,
            per_1m_cache_write_input=4.0,
        )
    )
    estimator = ModelTokenEstimator(
        token_upper_bound_per_byte=1,
        message_overhead=0,
        capability_overhead=0,
    )

    assert _worst_case_cost(
        info,
        _request(cache_mode="automatic-prefix"),
        estimator=estimator,
    ) == Decimal("0.000204")

    missing = _info(CostInfo(per_1m_input=1.0, per_1m_output=2.0))
    assert (
        _worst_case_cost(
            missing,
            _request(cache_mode="automatic-prefix"),
            estimator=estimator,
        )
        is None
    )
