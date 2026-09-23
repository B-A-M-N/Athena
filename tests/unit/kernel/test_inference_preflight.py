from types import SimpleNamespace

import pytest

from athena.kernel.inference_retry import _check_provider_input_allowance
from athena.models.router import ModelSelection
from athena.protocol.errors import ContextOverflow
from athena.protocol.models import ModelInfo, ModelRequest


class _WireBoundProvider:
    def __init__(self, bound: int):
        self.bound = bound

    def request_token_upper_bound(self, request, *, token_upper_bound_per_byte):
        return self.bound


def _request() -> ModelRequest:
    return ModelRequest(
        messages=(),
        model="small",
        provider="wire",
        request_id="request-1",
        max_tokens=20,
    )


def _selection() -> ModelSelection:
    return ModelSelection(
        provider="wire",
        model="small",
        info=ModelInfo(id="small", provider="wire", context_limit=100),
    )


def test_provider_wire_bound_is_checked_against_input_allowance():
    compiled = SimpleNamespace(
        requirements=SimpleNamespace(requested_output_tokens=20),
    )

    with pytest.raises(ContextOverflow, match="input allowance"):
        _check_provider_input_allowance(
            _WireBoundProvider(81),
            _request(),
            _selection(),
            compiled,
            token_upper_bound_per_byte=1,
        )


def test_provider_wire_bound_accepts_request_within_input_allowance():
    compiled = SimpleNamespace(
        requirements=SimpleNamespace(requested_output_tokens=20),
    )

    _check_provider_input_allowance(
        _WireBoundProvider(80),
        _request(),
        _selection(),
        compiled,
        token_upper_bound_per_byte=1,
    )
