from __future__ import annotations

import pytest

from athena.models.compat.anthropic_sdk import build_async_client


def test_anthropic_one_x_client_receives_custom_headers_and_explicit_proxy_policy():
    anthropic = pytest.importorskip("anthropic")
    client = build_async_client(
        anthropic,
        api_key="test-key",
        base_url="https://api.example.test",
        headers={"x-tenant": "tenant-a"},
        timeout=3.0,
        trust_env=False,
    )
    try:
        assert client._client.headers["x-tenant"] == "tenant-a"  # noqa: SLF001 - SDK seam
        assert client._client.trust_env is False  # noqa: SLF001 - SDK seam
    finally:
        import asyncio

        asyncio.run(client.close())


def test_anthropic_canonical_header_override_is_rejected():
    class LegacyClient:
        def __init__(self, *, api_key, base_url):
            del api_key, base_url

    class LegacySDK:
        AsyncAnthropic = LegacyClient

    with pytest.raises(ValueError, match="cannot honor configured headers"):
        build_async_client(
            LegacySDK,
            api_key="test-key",
            base_url="https://api.example.test",
            headers={"X-Vendor": "ok"},
            timeout=3.0,
            trust_env=False,
        )
