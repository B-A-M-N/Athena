"""Compatibility boundary for Anthropic SDK client construction."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
import importlib
from typing import Any


def build_async_client(
    sdk: Any,
    *,
    api_key: str | None,
    base_url: str,
    headers: Mapping[str, str],
    timeout: float,
    trust_env: bool,
) -> Any:
    """Construct a client with explicit transport and header semantics.

    Anthropic 1.x uses ``httpx2`` while the older supported API used regular
    ``httpx``.  The SDK itself is the compatibility authority; this module
    chooses the matching client type and refuses a version that cannot honor
    Athena's proxy or header policy.
    """
    parameters = inspect.signature(sdk.AsyncAnthropic).parameters
    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "base_url": base_url,
    }
    if "default_headers" in parameters:
        kwargs["default_headers"] = dict(headers)
    elif headers:
        raise ValueError("installed Anthropic SDK cannot honor configured headers")
    if "http_client" not in parameters:
        if not trust_env:
            raise ValueError("installed Anthropic SDK cannot honor Athena's proxy policy")
        return sdk.AsyncAnthropic(**kwargs)
    try:
        http_transport = importlib.import_module("httpx2")
    except ImportError:
        http_transport = importlib.import_module("httpx")
    kwargs["http_client"] = http_transport.AsyncClient(
        headers=dict(headers),
        timeout=float(timeout),
        trust_env=bool(trust_env),
    )
    return sdk.AsyncAnthropic(**kwargs)


__all__ = ["build_async_client"]
