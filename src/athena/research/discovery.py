"""Bounded, policy-aware source discovery adapters.

Discovery returns candidate metadata only.  The research capability remains
responsible for source admission, immutable capture, and evidence verification.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from collections.abc import Mapping
from typing import Any, Callable, Protocol, runtime_checkable
from urllib.parse import urlsplit

from athena.network import pinned_async_transport
from athena.research.policy import SourcePolicy, canonicalize_uri


@runtime_checkable
class ResearchDiscoveryProvider(Protocol):
    """Provider boundary for bounded candidate discovery."""

    name: str

    async def search(
        self, *, query: str, limit: int, context: Any = None, **kwargs: Any
    ) -> list[Mapping[str, Any]]: ...


class HttpDiscoveryProvider:
    """First-party JSON source-discovery adapter."""

    def __init__(
        self,
        endpoint: str,
        *,
        source_policy: SourcePolicy,
        timeout: float = 10.0,
        host_resolver=None,
        max_bytes: int = 1_000_000,
    ) -> None:
        self._endpoint = str(endpoint).strip()
        self._source_policy = source_policy
        self._timeout = max(0.1, float(timeout))
        self._host_resolver = host_resolver or socket.getaddrinfo
        self._max_bytes = max(1, min(int(max_bytes), 5_000_000))
        self.name = "http-index"

    async def search(self, *, query: str, limit: int, **_context: Any) -> list[dict[str, Any]]:
        canonical = self._source_policy.check(self._endpoint)
        parsed = urlsplit(canonical)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        infos = await _resolve_with_timeout(self._host_resolver, host, port, timeout=self._timeout)
        addresses = self._source_policy.check_resolved(host, [str(info[4][0]) for info in infos])
        import httpx

        transport = pinned_async_transport(host, addresses)
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "Athena-Research-Discovery/1"},
            transport=transport,
        ) as client:
            async with client.stream(
                "GET", canonical, params={"q": query, "limit": max(1, min(int(limit), 50))}
            ) as response:
                if response.status_code >= 300:
                    raise RuntimeError(f"discovery endpoint returned HTTP {response.status_code}")
                body = await response.aread()
        if len(body) > self._max_bytes:
            raise RuntimeError(f"discovery response exceeds max_bytes={self._max_bytes}")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("discovery endpoint returned invalid JSON") from exc
        if isinstance(payload, list):
            records = payload
        elif isinstance(payload, Mapping):
            records = payload.get("results", payload.get("candidates", payload.get("items")))
        else:
            records = None
        if not isinstance(records, list):
            raise RuntimeError("discovery response must contain a results array")
        return [dict(item) for item in records if isinstance(item, Mapping)]

    async def __call__(self, *, query: str, limit: int, **context: Any) -> list[dict[str, Any]]:
        return await self.search(query=query, limit=limit, **context)


class _ApiDiscoveryProvider:
    """Bounded JSON adapter for a fixed, operator-selected search API."""

    name: str = "api-search"

    async def search(self, *, query: str, limit: int, **_context: Any) -> list[dict[str, Any]]:
        raise NotImplementedError

    def __init__(
        self,
        *,
        endpoint: str,
        source_policy: SourcePolicy,
        api_key: str | None = None,
        api_key_resolver: Callable[[], str | None] | None = None,
        timeout: float = 10.0,
        host_resolver=None,
        max_bytes: int = 1_000_000,
    ) -> None:
        self._endpoint = str(endpoint).strip()
        self._source_policy = source_policy
        self._api_key = str(api_key).strip() if api_key else None
        self._api_key_resolver = api_key_resolver
        self._timeout = max(0.1, float(timeout))
        self._host_resolver = host_resolver or socket.getaddrinfo
        self._max_bytes = max(1, min(int(max_bytes), 5_000_000))

    def _resolve_api_key(self) -> str:
        value = self._api_key
        if value is None and self._api_key_resolver is not None:
            value = self._api_key_resolver()
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"{self.name} API credential is unavailable")
        return value.strip()

    async def _request_json(
        self,
        *,
        method: str,
        query: str,
        limit: int,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        endpoint = canonicalize_uri(self._endpoint)
        parsed = urlsplit(endpoint)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        endpoint_policy = SourcePolicy(
            allowed_domains=(host,),
            denied_domains=self._source_policy.denied_domains,
            allow_private_network=self._source_policy.allow_private_network,
        )
        endpoint = endpoint_policy.check(endpoint)
        infos = await _resolve_with_timeout(self._host_resolver, host, port, timeout=self._timeout)
        addresses = endpoint_policy.check_resolved(host, [str(info[4][0]) for info in infos])
        import httpx

        transport = pinned_async_transport(host, addresses)
        request_params = dict(params or {})
        request_params.setdefault("q", query)
        request_params.setdefault("limit", max(1, min(int(limit), 50)))
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "Athena-Research-Discovery/1", **dict(headers)},
            transport=transport,
        ) as client:
            async with client.stream(
                method,
                endpoint,
                params=request_params if method.upper() == "GET" else None,
                json=json_body,
            ) as response:
                if response.status_code >= 300:
                    raise RuntimeError(f"{self.name} search returned HTTP {response.status_code}")
                body = await response.aread()
        if len(body) > self._max_bytes:
            raise RuntimeError(f"{self.name} search response exceeds max_bytes={self._max_bytes}")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{self.name} search returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise RuntimeError(f"{self.name} search response must be an object")
        return payload

    async def __call__(self, *, query: str, limit: int, **context: Any) -> list[dict[str, Any]]:
        return await self.search(query=query, limit=limit, **context)


class BraveSearchProvider(_ApiDiscoveryProvider):
    """Brave Web Search adapter with no raw credential in request URLs."""

    name = "brave-search"

    def __init__(
        self,
        *,
        source_policy: SourcePolicy,
        api_key: str | None = None,
        api_key_resolver: Callable[[], str | None] | None = None,
        endpoint: str = "https://api.search.brave.com/res/v1/web/search",
        timeout: float = 10.0,
        host_resolver=None,
        max_bytes: int = 1_000_000,
    ) -> None:
        super().__init__(
            endpoint=endpoint,
            source_policy=source_policy,
            api_key=api_key,
            api_key_resolver=api_key_resolver,
            timeout=timeout,
            host_resolver=host_resolver,
            max_bytes=max_bytes,
        )

    async def search(self, *, query: str, limit: int, **_context: Any) -> list[dict[str, Any]]:
        payload = await self._request_json(
            method="GET",
            query=query,
            limit=limit,
            headers={"X-Subscription-Token": self._resolve_api_key()},
            params={"count": max(1, min(int(limit), 50))},
        )
        web = payload.get("web")
        results = web.get("results") if isinstance(web, Mapping) else None
        if not isinstance(results, list):
            return []
        return [
            {
                "uri": str(item.get("url") or ""),
                "title": str(item.get("title") or ""),
                "snippet": str(item.get("description") or ""),
                "source_type": "web",
            }
            for item in results[: max(1, min(int(limit), 50))]
            if isinstance(item, Mapping) and item.get("url")
        ]


class TavilySearchProvider(_ApiDiscoveryProvider):
    """Tavily Search adapter with bounded result metadata."""

    name = "tavily-search"

    def __init__(
        self,
        *,
        source_policy: SourcePolicy,
        api_key: str | None = None,
        api_key_resolver: Callable[[], str | None] | None = None,
        endpoint: str = "https://api.tavily.com/search",
        timeout: float = 10.0,
        host_resolver=None,
        max_bytes: int = 1_000_000,
    ) -> None:
        super().__init__(
            endpoint=endpoint,
            source_policy=source_policy,
            api_key=api_key,
            api_key_resolver=api_key_resolver,
            timeout=timeout,
            host_resolver=host_resolver,
            max_bytes=max_bytes,
        )

    async def search(self, *, query: str, limit: int, **_context: Any) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 50))
        payload = await self._request_json(
            method="POST",
            query=query,
            limit=bounded_limit,
            headers={},
            json_body={
                "api_key": self._resolve_api_key(),
                "query": query,
                "max_results": bounded_limit,
                "include_answer": False,
            },
        )
        results = payload.get("results")
        if not isinstance(results, list):
            return []
        return [
            {
                "uri": str(item.get("url") or ""),
                "title": str(item.get("title") or ""),
                "snippet": str(item.get("content") or ""),
                "source_type": "web",
            }
            for item in results[:bounded_limit]
            if isinstance(item, Mapping) and item.get("url")
        ]


async def _resolve_with_timeout(resolver, host: str, port: int, *, timeout: float):
    """Run blocking DNS resolution in a daemon thread with a hard timeout."""
    loop = asyncio.get_running_loop()
    result: asyncio.Future = loop.create_future()

    def finish(value: tuple[str, object]) -> None:
        if not result.done():
            result.set_result(value)

    def resolve() -> None:
        try:
            value = resolver(host, port, type=socket.SOCK_STREAM)
        except BaseException as exc:
            payload: tuple[str, object] = ("error", exc)
        else:
            payload = ("ok", value)
        try:
            loop.call_soon_threadsafe(finish, payload)
        except RuntimeError:
            pass

    threading.Thread(target=resolve, name="athena-dns-resolver", daemon=True).start()
    try:
        status, value = await asyncio.wait_for(result, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"discovery DNS resolution timed out after {timeout}s") from exc
    if status == "error":
        assert isinstance(value, BaseException)
        raise value
    return value


resolve_with_timeout = _resolve_with_timeout


__all__ = [
    "BraveSearchProvider",
    "HttpDiscoveryProvider",
    "ResearchDiscoveryProvider",
    "TavilySearchProvider",
]
