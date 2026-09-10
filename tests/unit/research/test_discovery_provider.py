from __future__ import annotations

import threading

import httpx
import pytest

from athena.capabilities.research import (
    BraveSearchProvider,
    HttpDiscoveryProvider,
    TavilySearchProvider,
)
from athena.research import discovery as discovery_module
from athena.research.policy import SourcePolicy, SourcePolicyError


@pytest.mark.asyncio
async def test_http_discovery_provider_returns_bounded_candidate_metadata(monkeypatch):
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"results": [{"uri": "https://docs.example.test/guide", "title": "Guide"}]},
        )

    monkeypatch.setattr(
        discovery_module,
        "pinned_async_transport",
        lambda host, addresses: httpx.MockTransport(handler),
    )

    provider = HttpDiscoveryProvider(
        "https://index.example.test/search",
        source_policy=SourcePolicy(allowed_domains=("index.example.test",)),
        host_resolver=lambda host, port, type: [(None, None, None, None, ("93.184.216.34", port))],
    )

    rows = await provider(query="release", limit=3)

    assert rows == [{"uri": "https://docs.example.test/guide", "title": "Guide"}]
    assert dict(requests[0].url.params) == {"q": "release", "limit": "3"}


@pytest.mark.asyncio
async def test_http_discovery_provider_rechecks_resolved_addresses(monkeypatch):
    monkeypatch.setattr(
        discovery_module,
        "pinned_async_transport",
        lambda host, addresses: httpx.MockTransport(
            lambda request: httpx.Response(200, json={"results": []})
        ),
    )
    provider = HttpDiscoveryProvider(
        "https://index.example.test/search",
        source_policy=SourcePolicy(allowed_domains=("index.example.test",)),
        host_resolver=lambda host, port, type: [(None, None, None, None, ("127.0.0.1", port))],
    )

    with pytest.raises(SourcePolicyError, match="private/local"):
        await provider(query="release", limit=3)


@pytest.mark.asyncio
async def test_http_discovery_provider_bounds_stuck_dns_resolution():
    started = threading.Event()
    release = threading.Event()

    def stuck_resolver(host, port, type):
        del host, port, type
        started.set()
        release.wait()
        return []

    provider = HttpDiscoveryProvider(
        "https://index.example.test/search",
        source_policy=SourcePolicy(allowed_domains=("index.example.test",)),
        timeout=0.1,
        host_resolver=stuck_resolver,
    )

    with pytest.raises(RuntimeError, match="DNS resolution timed out"):
        await provider(query="release", limit=3)
    assert started.is_set()
    release.set()


@pytest.mark.asyncio
async def test_brave_search_provider_uses_header_credential_and_maps_results(monkeypatch):
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"web": {"results": [{"url": "https://docs.example.test/a", "title": "A"}]}},
        )

    monkeypatch.setattr(
        discovery_module,
        "pinned_async_transport",
        lambda host, addresses: httpx.MockTransport(handler),
    )
    provider = BraveSearchProvider(
        source_policy=SourcePolicy(allowed_domains=("docs.example.test",)),
        api_key="brave-secret",
        host_resolver=lambda host, port, type: [(None, None, None, None, ("93.184.216.34", port))],
    )

    assert await provider.search(query="release", limit=2) == [
        {
            "uri": "https://docs.example.test/a",
            "title": "A",
            "snippet": "",
            "source_type": "web",
        }
    ]
    assert requests[0].headers["x-subscription-token"] == "brave-secret"
    assert "brave-secret" not in str(requests[0].url)


@pytest.mark.asyncio
async def test_tavily_search_provider_sends_bounded_json_body(monkeypatch):
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://docs.example.test/a",
                        "title": "A",
                        "content": "summary",
                    }
                ]
            },
        )

    monkeypatch.setattr(
        discovery_module,
        "pinned_async_transport",
        lambda host, addresses: httpx.MockTransport(handler),
    )
    provider = TavilySearchProvider(
        source_policy=SourcePolicy(allowed_domains=("docs.example.test",)),
        api_key="tavily-secret",
        host_resolver=lambda host, port, type: [(None, None, None, None, ("93.184.216.34", port))],
    )

    rows = await provider.search(query="release", limit=3)
    assert rows[0]["snippet"] == "summary"
    body = requests[0].content.decode("utf-8")
    assert '"max_results":3' in body
    assert '"api_key":"tavily-secret"' in body
