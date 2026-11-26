"""Provider-wire request bounds at the neutral model boundary."""

from __future__ import annotations

import json
from typing import Any

from athena.protocol.models import ModelRequest


def wire_payload_upper_bound(
    payload: Any,
    *,
    token_upper_bound_per_byte: int | None = 1,
    envelope_overhead: int = 256,
) -> int | None:
    """Bound a provider's serialized JSON request payload conservatively."""
    if token_upper_bound_per_byte is None:
        return None
    bound = int(token_upper_bound_per_byte)
    if bound < 1:
        raise ValueError("token_upper_bound_per_byte must be positive")
    if envelope_overhead < 0:
        raise ValueError("envelope_overhead must be non-negative")
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        allow_nan=False,
        separators=(", ", ": "),
    ).encode("ascii")
    return max(1, len(encoded) * bound + envelope_overhead)


def provider_request_upper_bound(
    provider: Any,
    request: ModelRequest,
    *,
    token_upper_bound_per_byte: int | None = 1,
) -> int | None:
    """Use an optional adapter-owned bound without changing the provider API."""
    method = getattr(provider, "request_token_upper_bound", None)
    if not callable(method):
        return None
    value = method(request, token_upper_bound_per_byte=token_upper_bound_per_byte)
    if value is None:
        return None
    result = int(value)
    if result < 0:
        raise ValueError("provider request token bound must be non-negative")
    return result


class WireRequestBounds:
    """Mixin for adapters that expose one canonical wire-payload builder."""

    def request_token_upper_bound(self, request, **kwargs):
        builder = getattr(self, "_build_request", None)
        builder_kwargs: dict[str, Any] = {}
        if builder is None:
            builder = getattr(self, "_build_kwargs")
            builder_kwargs["stream"] = True
        payload = builder(request, **builder_kwargs)
        return wire_payload_upper_bound(payload, **kwargs)


__all__ = ["WireRequestBounds", "provider_request_upper_bound", "wire_payload_upper_bound"]
