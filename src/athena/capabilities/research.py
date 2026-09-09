"""Durable research/evidence capability.

This capability deliberately stops at the epistemic boundary.  It records
source snapshots, evidence, claim links, and research gaps, and can verify an
excerpt against a captured artifact.  Network acquisition belongs to a
separate policy-controlled route; this module never fetches arbitrary URLs on
behalf of a model call.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import sqlite3
import threading
from collections.abc import Mapping
from typing import Any, Callable, Protocol, Sequence, runtime_checkable
from urllib.parse import urlsplit

from athena.network import pinned_async_transport
from athena.protocol.artifacts import parse_artifact_uri
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
    ResourceClass,
)
from athena.research.models import EvidenceObject, ResearchGap, SourceRecord
from athena.research.policy import (
    SourcePolicy,
    SourcePolicyError,
    canonicalize_uri,
    classify_source,
)

_SOURCE_TYPES = ("web", "paper", "documentation", "dataset", "code", "local")
_EVIDENCE_TYPES = ("quote", "measurement", "observation", "derivation", "execution")
_GAP_KINDS = (
    "unsupported_claim",
    "conflict",
    "stale_source",
    "source_quality",
    "unanswered_question",
)


@runtime_checkable
class ResearchDiscoveryProvider(Protocol):
    """Provider boundary for bounded candidate discovery.

    Providers return untrusted metadata only. The research capability remains
    the authority that applies source policy and performs immutable capture.
    """

    name: str

    async def search(
        self, *, query: str, limit: int, context: Any = None, **kwargs: Any
    ) -> list[Mapping[str, Any]]: ...


class HttpDiscoveryProvider:
    """First-party JSON source-discovery adapter.

    The endpoint is an index, not a source of truth. Its response is bounded
    and returned as candidate metadata; every candidate is still checked by
    ``ResearchCapability`` and must be separately acquired into an immutable
    snapshot before it can support a claim.
    """

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
        infos = await _resolve_with_timeout(
            self._host_resolver,
            host,
            port,
            timeout=self._timeout,
        )
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
        """Compatibility call surface for pre-provider deployments."""
        return await self.search(query=query, limit=limit, **context)


class _ApiDiscoveryProvider:
    """Bounded JSON adapter for a fixed, operator-selected search API.

    Search APIs are transport endpoints, not research sources. Their host is
    therefore checked for scheme, deny-list membership, and private-address
    resolution, while the returned candidate URLs still go through the
    normal ``ResearchCapability`` source allowlist before they are usable.
    """

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
        # The provider endpoint is fixed by the adapter implementation, but
        # still obeys the operator's denied/private-network controls. Candidate
        # result URLs remain subject to the stricter allowlist in the caller.
        endpoint_policy = SourcePolicy(
            allowed_domains=(host,),
            denied_domains=self._source_policy.denied_domains,
            allow_private_network=self._source_policy.allow_private_network,
        )
        endpoint = endpoint_policy.check(endpoint)
        infos = await _resolve_with_timeout(
            self._host_resolver,
            host,
            port,
            timeout=self._timeout,
        )
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
    """Run potentially blocking DNS resolution without leaking an executor.

    ``socket.getaddrinfo`` has no per-call timeout and the default asyncio
    executor is process-lifetime state. A daemon thread gives resolution a
    hard upper bound without allowing a stuck libc resolver to keep Athena
    alive during shutdown.
    """
    loop = asyncio.get_running_loop()
    result: asyncio.Future = loop.create_future()

    def finish(value: tuple[str, object]) -> None:
        if not result.done():
            result.set_result(value)

    def resolve() -> None:
        try:
            value = resolver(host, port, type=socket.SOCK_STREAM)
        except BaseException as exc:  # surface resolver failures to the caller
            payload: tuple[str, object] = ("error", exc)
        else:
            payload = ("ok", value)
        try:
            loop.call_soon_threadsafe(finish, payload)
        except RuntimeError:
            # The loop may close immediately after a timeout; the thread is
            # daemonized specifically so this late callback cannot pin exit.
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


class ResearchCapability:
    """Model-visible access to the durable Evidence/Research Fabric."""

    descriptor = CapabilityDescriptor(
        id="research",
        description=(
            "Durable evidence-backed research records: record and list source "
            "snapshots, discover bounded candidates, extract exact evidence, link evidence to Athena claims, "
            "track research gaps, search the local corpus, verify excerpts, and "
            "plan/assess/bundle explicit research requirements. Planning is "
            "deterministic and local; external fetching is a separate "
            "allowlisted operation."
        ),
        tags=frozenset(
            {
                "research",
                "evidence",
                "source",
                "sources",
                "verify",
                "verification",
                "web",
                "latest",
                "release",
            }
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "fetch",
                        "discover",
                        "record_source",
                        "sources",
                        "search",
                        "record_evidence",
                        "evidence",
                        "record_gap",
                        "gaps",
                        "close_gap",
                        "verify",
                        "plan",
                        "assess",
                        "bundle",
                        "run",
                    ],
                },
                "uri": {"type": "string", "minLength": 1, "maxLength": 4096},
                "title": {"type": "string", "maxLength": 1000},
                "source_type": {"type": "string", "enum": list(_SOURCE_TYPES)},
                "content": {"type": "string", "maxLength": 10_000_000},
                "artifact_uri": {"type": "string", "maxLength": 4096},
                "published_at": {"type": "string", "maxLength": 128},
                "source_id": {"type": "string", "maxLength": 128},
                "gap_id": {"type": "string", "maxLength": 128},
                "evidence_id": {"type": "string", "maxLength": 128},
                "claim_id": {"type": "string", "maxLength": 128},
                "claim": {"type": "string", "minLength": 1, "maxLength": 20_000},
                "excerpt": {"type": "string", "minLength": 1, "maxLength": 20_000},
                "locator": {"type": "object", "additionalProperties": True},
                "evidence_type": {"type": "string", "enum": list(_EVIDENCE_TYPES)},
                "extraction_method": {"type": "string", "maxLength": 128},
                "extraction_model": {"type": "string", "maxLength": 256},
                "receipt": {"type": "object", "additionalProperties": True},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "corroborates": {"type": "array", "items": {"type": "string"}},
                "contradicts": {"type": "array", "items": {"type": "string"}},
                "objective": {"type": "string", "minLength": 1, "maxLength": 20_000},
                "question": {"type": "string", "minLength": 1, "maxLength": 20_000},
                "requirements": {
                    "type": "array",
                    "maxItems": 50,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["question"],
                        "properties": {
                            "id": {"type": "string", "maxLength": 128},
                            "question": {"type": "string", "minLength": 1, "maxLength": 20_000},
                            "claim_id": {"type": "string", "maxLength": 128},
                            "kind": {"type": "string", "enum": list(_GAP_KINDS)},
                            "required": {"type": "boolean"},
                            "queries": {
                                "type": "array",
                                "maxItems": 5,
                                "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                            },
                        },
                    },
                },
                "queries": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                },
                "uris": {
                    "type": "array",
                    "maxItems": 50,
                    "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                },
                "source_specs": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["uri"],
                        "properties": {
                            "uri": {"type": "string", "minLength": 1, "maxLength": 4096},
                            "title": {"type": "string", "maxLength": 1000},
                            "source_type": {"type": "string", "enum": list(_SOURCE_TYPES)},
                            "content": {"type": "string", "maxLength": 10_000_000},
                            "artifact_uri": {"type": "string", "maxLength": 4096},
                            "published_at": {"type": "string", "maxLength": 128},
                            "metadata": {"type": "object", "additionalProperties": True},
                        },
                    },
                },
                "extractions": {
                    "type": "array",
                    "maxItems": 100,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["claim", "excerpt"],
                        "properties": {
                            "source_id": {"type": "string", "maxLength": 128},
                            "uri": {"type": "string", "maxLength": 4096},
                            "claim_id": {"type": "string", "maxLength": 128},
                            "claim": {"type": "string", "minLength": 1, "maxLength": 20_000},
                            "excerpt": {"type": "string", "minLength": 1, "maxLength": 20_000},
                            "locator": {"type": "object", "additionalProperties": True},
                            "evidence_type": {"type": "string", "enum": list(_EVIDENCE_TYPES)},
                            "extraction_method": {"type": "string", "maxLength": 128},
                            "extraction_model": {"type": "string", "maxLength": 256},
                            "receipt": {"type": "object", "additionalProperties": True},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "corroborates": {"type": "array", "items": {"type": "string"}},
                            "contradicts": {"type": "array", "items": {"type": "string"}},
                            "metadata": {"type": "object", "additionalProperties": True},
                        },
                    },
                },
                "gap_ids": {"type": "array", "items": {"type": "string", "maxLength": 128}},
                "kind": {"type": "string", "enum": list(_GAP_KINDS)},
                "required": {"type": "boolean"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "claim_ids": {"type": "array", "items": {"type": "string", "maxLength": 128}},
                "query": {"type": "string", "maxLength": 2000},
                "status": {"type": "string", "enum": ["OPEN", "CLOSED"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "autonomous": {"type": "boolean"},
                "max_research_rounds": {"type": "integer", "minimum": 1, "maximum": 3},
                "max_sources": {"type": "integer", "minimum": 1, "maximum": 20},
                "max_queries": {"type": "integer", "minimum": 1, "maximum": 20},
                "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 30},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 10_000_000},
                "max_research_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50_000_000,
                },
                "metadata": {"type": "object", "additionalProperties": True},
            },
            "additionalProperties": False,
        },
        effects=frozenset(
            {
                EffectClass.READ_LOCAL,
                EffectClass.WRITE_LOCAL,
                EffectClass.NETWORK_READ,
            }
        ),
        resources=frozenset({ResourceClass.RESEARCH}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(
        self,
        store,
        *,
        artifact_store=None,
        source_policy: SourcePolicy | None = None,
        host_resolver=None,
        discovery_provider=None,
        discovery_providers: Sequence[ResearchDiscoveryProvider | Any] | None = None,
        utility_inference: Callable[..., Any] | None = None,
    ) -> None:
        self._store = store
        self._artifacts = artifact_store
        self._source_policy = source_policy or SourcePolicy()
        self._host_resolver = host_resolver or socket.getaddrinfo
        configured = tuple(discovery_providers or ())
        if discovery_provider is not None and discovery_provider not in configured:
            configured = (*configured, discovery_provider)
        self._discovery_providers = configured
        self._utility_inference = utility_inference

    async def invoke(self, request: CapabilityRequest, **kw) -> CapabilityResult:
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")
        context = kw.get("context")
        if self._store is None:
            return _result(request, ok=False, error="research store not available")
        try:
            if operation == "fetch":
                return await self._fetch(request, args, context)
            if operation == "discover":
                return await self._discover(request, args, context)
            if operation == "record_source":
                return await self._record_source(request, args, context)
            if operation == "sources":
                return await self._sources(request, args, context)
            if operation == "search":
                return await self._search(request, args, context)
            if operation == "record_evidence":
                return await self._record_evidence(request, args, context)
            if operation == "evidence":
                return await self._evidence(request, args, context)
            if operation == "record_gap":
                return await self._record_gap(request, args)
            if operation == "gaps":
                return await self._gaps(request, args)
            if operation == "close_gap":
                return await self._close_gap(request, args, context)
            if operation == "verify":
                return await self._verify(request, args, context)
            if operation == "plan":
                return await self._plan(request, args, context)
            if operation == "assess":
                return await self._assess(request, args, context)
            if operation == "bundle":
                return await self._bundle(request, args, context)
            if operation == "run":
                return await self._run(request, args, context)
            return _result(request, ok=False, error=f"unknown operation: {operation}")
        except (KeyError, SourcePolicyError, ValueError) as exc:
            return _result(request, ok=False, error=str(exc))
        except (OSError, RuntimeError, TypeError, sqlite3.Error) as exc:
            # Capability failures are model-visible, but unexpected storage
            # errors remain explicit rather than becoming false evidence.
            return _result(request, ok=False, error=f"research operation failed: {exc}")

    async def _discover(self, request, args, context) -> CapabilityResult:
        """Discover bounded, policy-valid source candidates.

        The default provider is the durable task/project corpus. Deployments
        may inject a provider for external indexes, but its results still
        pass SourcePolicy before they become model-visible candidates. This
        operation never fetches a candidate; acquisition remains ``fetch``.
        """
        query = str(args.get("query") or "").strip()
        if not query:
            return _result(request, ok=False, error="discover requires query")
        limit = min(int(args.get("limit") or 20), 50)
        workspace = getattr(context, "workspace", None)
        project_id = getattr(workspace, "id", None)
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()

        for hit in await self._search_content(
            query,
            task_id=request.task_id,
            project_id=project_id,
            limit=limit,
        ):
            source = hit.get("source") if isinstance(hit, Mapping) else None
            if not isinstance(source, Mapping):
                continue
            source_id = str(source.get("id") or "")
            uri = str(source.get("canonical_uri") or "")
            key = uri or source_id
            if not key or key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "uri": uri,
                    "source_id": source_id or None,
                    "title": str(source.get("title") or ""),
                    "source_type": str(source.get("source_type") or ""),
                    "snippet": str(hit.get("snippet") or "")[:2000],
                    "origin": "local_corpus",
                }
            )

        list_sources = getattr(self._store, "list_sources", None)
        if callable(list_sources) and len(candidates) < limit:
            sources = await list_sources(
                task_id=request.task_id,
                project_id=project_id,
                query=query,
                limit=limit,
            )
            for source in sources:
                record = source.to_record() if hasattr(source, "to_record") else source
                if not isinstance(record, Mapping):
                    continue
                source_id = str(record.get("id") or "")
                uri = str(record.get("canonical_uri") or "")
                key = uri or source_id
                if not key or key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    {
                        "uri": uri,
                        "source_id": source_id or None,
                        "title": str(record.get("title") or ""),
                        "source_type": str(record.get("source_type") or ""),
                        "snippet": "",
                        "origin": "local_catalog",
                    }
                )
                if len(candidates) >= limit:
                    break

        rejected: list[dict[str, str]] = []
        provider_status: list[dict[str, Any]] = []
        if self._discovery_providers:
            network_policy = getattr(workspace, "network_policy", None)
            if getattr(network_policy, "value", network_policy) == "deny":
                return _result(request, ok=False, error="network denied by workspace policy")
            for provider in self._discovery_providers:
                provider_name = str(
                    getattr(provider, "name", None)
                    or getattr(provider, "provider_name", None)
                    or type(provider).__name__
                )
                try:
                    search = getattr(provider, "search", None)
                    if callable(search):
                        provided = search(
                            query=query,
                            limit=limit,
                            task_id=request.task_id,
                            project_id=project_id,
                            context=context,
                        )
                    elif callable(provider):
                        provided = provider(
                            query=query,
                            limit=limit,
                            task_id=request.task_id,
                            project_id=project_id,
                            context=context,
                        )
                    else:
                        raise TypeError("provider has no search() or callable interface")
                    if asyncio.iscoroutine(provided):
                        provided = await provided
                    if not isinstance(provided, list):
                        raise TypeError("provider returned a non-list")
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    provider_status.append(
                        {"provider": provider_name, "status": "failed", "reason": str(exc)[:600]}
                    )
                    continue
                provider_status.append(
                    {"provider": provider_name, "status": "ok", "candidates": len(provided)}
                )
                for item in provided[:limit]:
                    if not isinstance(item, Mapping):
                        rejected.append(
                            {"provider": provider_name, "reason": "candidate is not an object"}
                        )
                        continue
                    uri = str(item.get("uri") or item.get("url") or "")
                    try:
                        canonical = self._source_policy.check(uri)
                    except SourcePolicyError as exc:
                        rejected.append(
                            {"provider": provider_name, "uri": uri[:4096], "reason": str(exc)}
                        )
                        continue
                    if canonical in seen:
                        continue
                    seen.add(canonical)
                    candidates.append(
                        {
                            "uri": canonical,
                            "title": str(item.get("title") or "")[:1000],
                            "source_type": str(item.get("source_type") or "web"),
                            "snippet": str(item.get("snippet") or "")[:2000],
                            "origin": provider_name,
                        }
                    )
                    if len(candidates) >= limit:
                        break

        return _result(
            request,
            output=_json(
                {
                    "query": query,
                    "candidates": candidates[:limit],
                    "provider": "configured" if self._discovery_providers else "local_corpus",
                    "providers": provider_status,
                    "network_used": bool(self._discovery_providers),
                    "rejected": rejected[:limit],
                }
            ),
        )

    async def _fetch(self, request, args, context) -> CapabilityResult:
        """Fetch one allowlisted source and persist its immutable snapshot.

        This is intentionally a small acquisition primitive, not a crawler.
        Redirects are not followed: a redirect target is a new URL that must
        pass SourcePolicy independently. Response bodies are bounded before
        artifact persistence so an external source cannot exhaust task state.
        """
        if not request.task_id:
            return _result(request, ok=False, error="fetch requires a task")
        canonical = self._source_policy.check(str(args.get("uri") or ""))
        if canonical.startswith("artifact://"):
            return _result(
                request,
                ok=False,
                error="fetch accepts http/https; use record_source for artifacts",
            )
        network_policy = getattr(getattr(context, "workspace", None), "network_policy", None)
        if getattr(network_policy, "value", network_policy) == "deny":
            return _result(request, ok=False, error="network denied by workspace policy")
        if self._artifacts is None:
            return _result(request, ok=False, error="artifact store not available")

        parsed = urlsplit(canonical)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        timeout = min(float(args.get("timeout") or 15.0), 30.0)
        try:
            infos = await _resolve_with_timeout(
                self._host_resolver,
                host,
                port,
                timeout=timeout,
            )
            addresses = [str(info[4][0]) for info in infos]
            resolved_addresses = self._source_policy.check_resolved(host, addresses)
        except (OSError, SourcePolicyError) as exc:
            return _result(request, ok=False, error=f"source DNS check failed: {exc}")

        import httpx

        max_bytes = min(int(args.get("max_bytes") or 2_000_000), 10_000_000)
        chunks: list[bytes] = []
        size = 0
        try:
            transport = pinned_async_transport(host, resolved_addresses)
            async with (
                httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=False,
                    trust_env=False,
                    headers={"User-Agent": "Athena-Research/1"},
                    transport=transport,
                ) as client,
                client.stream("GET", canonical) as response,
            ):
                if response.status_code >= 300:
                    location = response.headers.get("location")
                    suffix = f" location={location}" if location else ""
                    return _result(
                        request,
                        ok=False,
                        error=f"source fetch returned HTTP {response.status_code}{suffix}",
                        metadata={"status_code": response.status_code},
                    )
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        return _result(
                            request,
                            ok=False,
                            error=f"source exceeds max_bytes={max_bytes}",
                            metadata={"status_code": response.status_code},
                        )
                    chunks.append(chunk)
                media_type = (
                    response.headers.get("content-type", "application/octet-stream")
                    .split(";", 1)[0]
                    .strip()
                    or "application/octet-stream"
                )
                status_code = response.status_code
        except httpx.HTTPError as exc:
            return _result(request, ok=False, error=f"source fetch failed: {exc}")

        content = b"".join(chunks)
        ref = await self._artifacts.save(
            task_id=request.task_id,
            content=content,
            mime_type=media_type,
            producer="research.fetch",
            metadata={"source_uri": canonical, "status_code": status_code},
        )
        source = SourceRecord.for_uri(
            canonical,
            title=str(args.get("title") or ""),
            source_type=str(args.get("source_type") or "web"),
            authority_class=classify_source(canonical),
            content_hash=hashlib.sha256(content).hexdigest(),
            artifact_uri=ref.uri,
            published_at=args.get("published_at"),
            task_id=request.task_id,
            project_id=getattr(getattr(context, "workspace", None), "id", None),
            metadata={
                **dict(args.get("metadata") or {}),
                "status_code": status_code,
                "mime_type": media_type,
                "bytes": len(content),
                "resolved_addresses": list(resolved_addresses),
            },
        )
        await self._store.save_source(source)
        await _index_snapshot(self._store, source, content, mime_type=media_type)
        return _result(
            request,
            output=_json({"source": source.to_record()}),
            metadata={
                "status_code": status_code,
                "bytes": len(content),
                "resolved_addresses": list(resolved_addresses),
            },
        )

    async def _record_source(self, request, args, context) -> CapabilityResult:
        if not request.task_id:
            return _result(request, ok=False, error="record_source requires a task")
        canonical = self._source_policy.check(str(args.get("uri") or ""))
        content = args.get("content")
        artifact_uri = args.get("artifact_uri")
        content_hash = None
        snapshot: bytes | str | None = None
        if content is not None and artifact_uri is not None:
            return _result(
                request,
                ok=False,
                error="provide source content or artifact_uri, not both",
            )
        if content is not None:
            if self._artifacts is None:
                return _result(request, ok=False, error="artifact store not available")
            data = str(content).encode("utf-8")
            snapshot = data
            content_hash = hashlib.sha256(data).hexdigest()
            ref = await self._artifacts.save(
                task_id=request.task_id,
                content=data,
                mime_type="text/plain",
                producer="research.source",
                metadata={"source_uri": canonical},
            )
            artifact_uri = ref.uri
        elif artifact_uri is not None:
            if self._artifacts is None:
                return _result(request, ok=False, error="artifact store not available")
            if parse_artifact_uri(str(artifact_uri)) is None:
                return _result(request, ok=False, error="artifact_uri is not an artifact URI")
            if not await _artifact_visible(self._artifacts, str(artifact_uri), request.task_id):
                return _result(
                    request, ok=False, error="artifact snapshot is not visible to this task"
                )
            loaded_snapshot = await self._artifacts.load(str(artifact_uri))
            if not isinstance(loaded_snapshot, bytes):
                return _result(request, ok=False, error="artifact snapshot is not bytes")
            snapshot = loaded_snapshot
            content_hash = hashlib.sha256(loaded_snapshot).hexdigest()
        source = SourceRecord.for_uri(
            canonical,
            title=str(args.get("title") or ""),
            source_type=str(args.get("source_type") or "web"),
            authority_class=classify_source(canonical),
            content_hash=content_hash,
            artifact_uri=artifact_uri,
            published_at=args.get("published_at"),
            task_id=request.task_id,
            project_id=getattr(getattr(context, "workspace", None), "id", None),
            metadata=args.get("metadata") or {},
        )
        await self._store.save_source(source)
        await _index_snapshot(
            self._store,
            source,
            snapshot,
            mime_type=str((args.get("metadata") or {}).get("mime_type") or "text/plain"),
        )
        return _result(request, output=_json({"source": source.to_record()}))

    async def _sources(self, request, args, context) -> CapabilityResult:
        workspace = getattr(context, "workspace", None)
        sources = await self._store.list_sources(
            task_id=request.task_id,
            project_id=getattr(workspace, "id", None),
            query=args.get("query"),
            limit=int(args.get("limit") or 50),
        )
        return _result(request, output=_json({"sources": [s.to_record() for s in sources]}))

    async def _search(self, request, args, context) -> CapabilityResult:
        """Search captured local records without acquiring new network data."""
        query = str(args.get("query") or "").strip()
        if not query:
            return _result(request, ok=False, error="search requires query")
        workspace_id = getattr(getattr(context, "workspace", None), "id", None)
        sources = await self._store.list_sources(
            task_id=request.task_id,
            project_id=workspace_id,
            query=query,
            limit=int(args.get("limit") or 50),
        )
        evidence = await self._store.list_evidence(
            task_id=request.task_id,
            project_id=workspace_id,
            query=query,
            limit=int(args.get("limit") or 50),
        )
        content_hits = await self._search_content(
            query,
            task_id=request.task_id,
            project_id=workspace_id,
            limit=int(args.get("limit") or 50),
        )
        return _result(
            request,
            output=_json(
                {
                    "query": query,
                    "sources": [source.to_record() for source in sources],
                    "evidence": [item.to_record() for item in evidence],
                    "content_hits": content_hits,
                }
            ),
        )

    async def _search_content(
        self,
        query: str,
        *,
        task_id: str | None,
        project_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        search = getattr(self._store, "search_content", None)
        if search is None:
            return []
        return await search(
            query,
            task_id=task_id,
            project_id=project_id,
            limit=limit,
        )

    async def _record_evidence(self, request, args, context) -> CapabilityResult:
        if not request.task_id:
            return _result(request, ok=False, error="record_evidence requires a task")
        source_id = str(args.get("source_id") or "")
        source = await self._store.get_source(source_id)
        if source is None:
            return _result(request, ok=False, error=f"unknown source: {source_id}")
        # Source IDs are not bearer tokens. A task may cite its own capture or
        # a source explicitly promoted to its project, but not another task's
        # private source. Apply the same rule to relation targets below.
        if not _source_visible(source, request, context):
            return _result(request, ok=False, error=f"unknown source: {source_id}")
        related_ids = tuple(args.get("corroborates") or ()) + tuple(args.get("contradicts") or ())
        for related_id in related_ids:
            related = await self._store.get_evidence(str(related_id))
            if related is None or not await _evidence_visible(
                related, request, context, self._store.get_source
            ):
                return _result(
                    request,
                    ok=False,
                    error=f"related evidence is not visible: {related_id}",
                )
        metadata = dict(args.get("metadata") or {})
        receipt = args.get("receipt")
        if receipt is not None:
            if not isinstance(receipt, Mapping):
                return _result(request, ok=False, error="receipt must be an object")
            metadata["receipt"] = dict(receipt)
        evidence = EvidenceObject.for_content(
            source_id=source_id,
            extracted_claim=str(args.get("claim") or ""),
            exact_supporting_excerpt=str(args.get("excerpt") or ""),
            locator=args.get("locator") or {},
            evidence_type=str(args.get("evidence_type") or "quote"),
            # Authority is derived from the source, not model input.
            authority_class=source.authority_class,
            extraction_method=str(args.get("extraction_method") or "model"),
            extraction_model=args.get("extraction_model"),
            confidence=args.get("confidence"),
            task_id=request.task_id,
            claim_id=args.get("claim_id"),
            corroborates=tuple(args.get("corroborates") or ()),
            contradicts=tuple(args.get("contradicts") or ()),
            metadata=metadata,
        )
        await self._store.save_evidence(evidence)
        return _result(request, output=_json({"evidence": evidence.to_record()}))

    async def _evidence(self, request, args, context) -> CapabilityResult:
        records = await self._store.list_evidence(
            task_id=request.task_id,
            project_id=getattr(getattr(context, "workspace", None), "id", None),
            source_id=args.get("source_id"),
            claim_id=args.get("claim_id"),
            query=args.get("query"),
            limit=int(args.get("limit") or 50),
        )
        return _result(request, output=_json({"evidence": [e.to_record() for e in records]}))

    async def _record_gap(self, request, args) -> CapabilityResult:
        if not request.task_id:
            return _result(request, ok=False, error="record_gap requires a task")
        gap = ResearchGap.create(
            str(args.get("objective") or ""),
            str(args.get("question") or ""),
            kind=str(args.get("kind") or "unsupported_claim"),
            required=bool(args.get("required", True)),
            task_id=request.task_id,
            metadata=args.get("metadata") or {},
        )
        await self._store.save_gap(gap)
        return _result(request, output=_json({"gap": gap.to_record()}))

    async def _gaps(self, request, args) -> CapabilityResult:
        gaps = await self._store.list_gaps(
            task_id=request.task_id,
            status=args.get("status"),
            limit=int(args.get("limit") or 100),
        )
        return _result(request, output=_json({"gaps": [g.to_record() for g in gaps]}))

    async def _close_gap(self, request, args, context) -> CapabilityResult:
        if not request.task_id:
            return _result(request, ok=False, error="close_gap requires a task")
        gap_id = str(args.get("gap_id") or "")
        if not gap_id:
            return _result(request, ok=False, error="close_gap requires gap_id")
        for evidence_id in tuple(args.get("evidence_ids") or ()):
            evidence = await self._store.get_evidence(str(evidence_id))
            if evidence is None or not await _evidence_visible(
                evidence, request, context, self._store.get_source
            ):
                return _result(
                    request,
                    ok=False,
                    error=f"evidence is not visible: {evidence_id}",
                )
        gap = await self._store.close_gap(
            gap_id, evidence_ids=tuple(args.get("evidence_ids") or ()), task_id=request.task_id
        )
        if gap is None:
            return _result(request, ok=False, error=f"unknown gap: {gap_id}")
        return _result(request, output=_json({"gap": gap.to_record()}))

    async def _verify(self, request, args, context) -> CapabilityResult:
        if not request.task_id:
            return _result(request, ok=False, error="verify requires a task")
        evidence_id = str(args.get("evidence_id") or "")
        evidence = await self._store.get_evidence(evidence_id)
        if evidence is None:
            return _result(request, ok=False, error=f"unknown evidence: {evidence_id}")
        source = await self._store.get_source(evidence.source_id)
        if source is None:
            return _result(request, output=_json({"status": "invalid", "reason": "source missing"}))
        if not _source_visible(source, request, context) or not await _evidence_visible(
            evidence, request, context, self._store.get_source
        ):
            return _result(request, ok=False, error=f"unknown evidence: {evidence_id}")
        verification = await self._verify_evidence(evidence, source)
        return _result(
            request,
            output=_json(
                {
                    **verification,
                    "evidence_id": evidence.id,
                    "source_id": source.id,
                }
            ),
        )

    async def _plan(self, request, args, context) -> CapabilityResult:
        """Persist a bounded research plan and retrieve local candidates.

        Planning is intentionally deterministic.  It creates durable gaps for
        explicit requirements and searches only already captured snapshots;
        acquisition remains the separate policy-controlled ``fetch`` route.
        """
        if not request.task_id:
            return _result(request, ok=False, error="plan requires a task")
        objective = str(args.get("objective") or "").strip()
        raw_requirements = args.get("requirements")
        if not objective:
            return _result(request, ok=False, error="plan requires objective")
        if not isinstance(raw_requirements, list) or not raw_requirements:
            return _result(request, ok=False, error="plan requires requirements")
        global_queries = _strings(args.get("queries"), limit=10)
        plan_input = {
            "task_id": request.task_id,
            "objective": objective,
            "requirements": raw_requirements,
            "queries": global_queries,
        }
        plan_id = (
            "plan_"
            + hashlib.sha256(
                json.dumps(plan_input, sort_keys=True, separators=(",", ":"), default=str).encode()
            ).hexdigest()[:24]
        )
        planned: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_requirements):
            if not isinstance(raw, Mapping):
                return _result(request, ok=False, error="plan requirements must be objects")
            question = str(raw.get("question") or "").strip()
            if not question:
                return _result(request, ok=False, error="each plan requirement needs a question")
            requirement_id = str(raw.get("id") or f"requirement-{index + 1}")
            claim_id = str(raw.get("claim_id") or "").strip() or None
            queries = _strings(raw.get("queries"), limit=5) or global_queries or [question]
            metadata = {
                **dict(args.get("metadata") or {}),
                "plan_id": plan_id,
                "requirement_id": requirement_id,
                "claim_id": claim_id,
                "queries": queries,
            }
            gap = ResearchGap.create(
                objective,
                question,
                kind=str(raw.get("kind") or args.get("kind") or "unsupported_claim"),
                required=bool(raw.get("required", True)),
                task_id=request.task_id,
                metadata=metadata,
            )
            await self._store.save_gap(gap)
            candidates: list[dict[str, Any]] = []
            for query in queries:
                candidates.extend(
                    await self._search_content(
                        query,
                        task_id=request.task_id,
                        project_id=getattr(getattr(context, "workspace", None), "id", None),
                        limit=5,
                    )
                )
            planned.append(
                {
                    "id": requirement_id,
                    "claim_id": claim_id,
                    "question": question,
                    "gap": gap.to_record(),
                    "queries": queries,
                    "candidate_count": len(candidates),
                    "candidates": _unique_candidates(candidates),
                }
            )
        return _result(
            request,
            output=_json(
                {
                    "plan_id": plan_id,
                    "objective": objective,
                    "requirements": planned,
                }
            ),
        )

    async def _assess(self, request, args, context) -> CapabilityResult:
        """Assess captured evidence and close only durably verified gaps."""
        if not request.task_id:
            return _result(request, ok=False, error="assess requires a task")
        gap_ids = {str(value) for value in args.get("gap_ids") or ()}
        requested_evidence = {str(value) for value in args.get("evidence_ids") or ()}
        requested_claims = {str(value) for value in args.get("claim_ids") or ()}
        workspace_id = getattr(getattr(context, "workspace", None), "id", None)
        gaps = await self._store.list_gaps(task_id=request.task_id, limit=200)
        evidence = await self._store.list_evidence(
            task_id=request.task_id,
            project_id=workspace_id,
            limit=200,
        )
        visible: list[EvidenceObject] = []
        for item in evidence:
            if await _evidence_visible(item, request, context, self._store.get_source):
                visible.append(item)
        by_id = {item.id: item for item in visible}
        assessed: list[dict[str, Any]] = []
        for gap in gaps:
            if gap_ids and gap.id not in gap_ids:
                assessed.append(gap.to_record())
                continue
            metadata = dict(gap.metadata)
            requirement_id = str(metadata.get("requirement_id") or "")
            claim_id = str(metadata.get("claim_id") or "")
            candidates = [
                item
                for item in visible
                if (
                    (requested_evidence and item.id in requested_evidence)
                    or (requested_claims and item.claim_id in requested_claims)
                    or (claim_id and item.claim_id == claim_id)
                    or (requirement_id and item.metadata.get("requirement_id") == requirement_id)
                )
            ]
            checks: list[dict[str, Any]] = []
            for item in candidates:
                source = await self._store.get_source(item.source_id)
                if source is not None:
                    checks.append(
                        {
                            "evidence_id": item.id,
                            **await self._verify_evidence(item, source),
                        }
                    )
            candidate_ids = {item.id for item in candidates}
            conflicts = [
                item.id
                for item in candidates
                if any(
                    related_id in candidate_ids
                    or (
                        related_id in by_id
                        and by_id[related_id].claim_id is not None
                        and by_id[related_id].claim_id == item.claim_id
                    )
                    for related_id in item.contradicts
                )
            ]
            verified = [
                check["evidence_id"] for check in checks if check.get("status") == "verified"
            ]
            can_close = (
                bool(candidates)
                and bool(verified)
                and not conflicts
                and all(check.get("status") == "verified" for check in checks)
            )
            updated = gap
            if gap.status == "OPEN" and can_close:
                updated = (
                    await self._store.close_gap(
                        gap.id,
                        evidence_ids=tuple(verified),
                        task_id=request.task_id,
                    )
                    or gap
                )
            record = updated.to_record()
            record["assessment"] = {
                "candidate_evidence_ids": [item.id for item in candidates],
                "verification": checks,
                "verified_evidence_ids": verified,
                "conflicts": conflicts,
                "closed_now": updated.status == "CLOSED" and gap.status != "CLOSED",
            }
            assessed.append(record)
        required_open = [
            record["id"]
            for record in assessed
            if record.get("required", True) and record.get("status") != "CLOSED"
        ]
        return _result(
            request,
            output=_json(
                {
                    "ready": not required_open,
                    "required_open_gaps": required_open,
                    "gaps": assessed,
                }
            ),
        )

    async def _bundle(self, request, args, context) -> CapabilityResult:
        """Return a bounded, task-scoped research packet for synthesis/judgment."""
        if not request.task_id:
            return _result(request, ok=False, error="bundle requires a task")
        workspace_id = getattr(getattr(context, "workspace", None), "id", None)
        limit = int(args.get("limit") or 50)
        sources = await self._store.list_sources(
            task_id=request.task_id,
            project_id=workspace_id,
            limit=limit,
        )
        evidence = await self._store.list_evidence(
            task_id=request.task_id,
            project_id=workspace_id,
            limit=limit,
        )
        gaps = await self._store.list_gaps(task_id=request.task_id, limit=200)
        required_open = [gap.id for gap in gaps if gap.required and gap.status != "CLOSED"]
        unverified_closed: list[str] = []
        for gap in gaps:
            if gap.status != "CLOSED" or not gap.required:
                continue
            if not gap.evidence_ids:
                unverified_closed.append(gap.id)
                continue
            for evidence_id in gap.evidence_ids:
                item = await self._store.get_evidence(evidence_id)
                source = await self._store.get_source(item.source_id) if item else None
                if (
                    item is None
                    or source is None
                    or (await self._verify_evidence(item, source))["status"] != "verified"
                ):
                    unverified_closed.append(gap.id)
                    break
        return _result(
            request,
            output=_json(
                {
                    "ready": not required_open and not unverified_closed,
                    "required_open_gaps": required_open,
                    "unverified_closed_gaps": unverified_closed,
                    "sources": [source.to_record() for source in sources],
                    "evidence": [item.to_record() for item in evidence],
                    "gaps": [gap.to_record() for gap in gaps],
                }
            ),
        )

    async def _autonomous_acquire(
        self,
        request,
        context,
        requirements: Sequence[Mapping[str, Any]],
        args: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        """Run bounded discovery/acquisition over the existing research primitives.

        This is deliberately not a second reasoning authority. The kernel or
        caller supplies the research questions; this helper only performs a
        finite discover -> policy-check -> immutable-fetch loop. Evidence
        extraction still requires explicit excerpts (and therefore remains
        verifiable rather than inferred from search snippets).
        """
        max_rounds = max(1, min(int(args.get("max_research_rounds") or 1), 3))
        max_sources = max(1, min(int(args.get("max_sources") or 5), 20))
        max_queries = max(1, min(int(args.get("max_queries") or 10), 20))
        max_bytes = max(1, min(int(args.get("max_research_bytes") or 20_000_000), 50_000_000))
        queries = _strings(args.get("queries"), limit=max_queries)
        for requirement in requirements:
            queries.extend(_strings(requirement.get("queries"), limit=5))
            if not requirement.get("queries") and requirement.get("question"):
                queries.append(str(requirement["question"]))
        queries = _unique_strings(queries)[:max_queries]

        captures: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        seen_uris: set[str] = set()
        discovered = 0
        fetched = 0
        bytes_fetched = 0
        rounds_run = 0
        attempted_queries: set[str] = set()
        for round_number in range(max_rounds):
            if fetched >= max_sources or bytes_fetched >= max_bytes:
                break
            rounds_run += 1
            round_queries = [query for query in queries if query not in attempted_queries]
            if not round_queries:
                # Adapt from durable gaps after the prior round's failed or
                # contradictory evidence. Utility inference is advisory and
                # bounded; deterministic gap questions remain the fallback.
                gaps = await self._store.list_gaps(task_id=request.task_id, limit=200)
                gap_context = [
                    {
                        "id": gap.id,
                        "question": gap.question,
                        "metadata": dict(gap.metadata),
                    }
                    for gap in gaps
                    if gap.status == "OPEN" and gap.required
                ][:50]
                inferred: Any = None
                if round_number + 1 < max_rounds and self._utility_inference is not None:
                    try:
                        prompt = json.dumps(
                            {
                                "remaining_gaps": gap_context,
                                "attempted_queries": sorted(attempted_queries),
                                "failed_queries": errors[-50:],
                            },
                            sort_keys=True,
                            default=str,
                        )
                        inferred = self._utility_inference(
                            system_prompt=(
                                "Return only a JSON array of at most 10 better research queries. "
                                "Do not claim evidence or answer the gaps."
                            ),
                            user_prompt=prompt,
                            role="summarizer",
                            task_id=request.task_id,
                            metadata={"purpose": "research_query_utility_inference"},
                        )
                        if asyncio.iscoroutine(inferred):
                            inferred = await asyncio.wait_for(inferred, timeout=10.0)
                    except Exception as exc:  # advisory path; deterministic fallback remains
                        errors.append({"utility_inference": str(exc)[:600]})
                        inferred = None
                parsed_queries: list[str] = []
                if isinstance(inferred, str):
                    try:
                        decoded = json.loads(inferred)
                        if isinstance(decoded, list):
                            parsed_queries = _strings(decoded, limit=max_queries)
                    except json.JSONDecodeError:
                        parsed_queries = []
                if not parsed_queries:
                    parsed_queries = _unique_strings(
                        [str(item.get("question") or "") for item in gap_context]
                    )[:max_queries]
                round_queries = [
                    query for query in parsed_queries if query not in attempted_queries
                ]
            for query in round_queries:
                attempted_queries.add(query)
                discovered_result = await self._discover(
                    request,
                    {"query": query, "limit": min(20, max_sources)},
                    context,
                )
                if discovered_result.status is not CapabilityResultStatus.OK:
                    errors.append({"query": query, "error": discovered_result.error})
                    continue
                payload = _decode_object(discovered_result.output)
                candidates = payload.get("candidates")
                if not isinstance(candidates, list):
                    continue
                discovered += len(candidates)
                for candidate in candidates:
                    if (
                        fetched >= max_sources
                        or bytes_fetched >= max_bytes
                        or not isinstance(candidate, Mapping)
                    ):
                        break
                    uri = str(candidate.get("uri") or "")
                    if not uri or uri in seen_uris or not uri.startswith(("http://", "https://")):
                        continue
                    seen_uris.add(uri)
                    fetched_result = await self._fetch(
                        request,
                        {
                            "uri": uri,
                            "title": candidate.get("title"),
                            "source_type": candidate.get("source_type") or "web",
                            "max_bytes": min(10_000_000, max_bytes - bytes_fetched),
                        },
                        context,
                    )
                    if fetched_result.status is not CapabilityResultStatus.OK:
                        errors.append({"uri": uri, "error": fetched_result.error})
                        continue
                    source = _decode_object(fetched_result.output).get("source")
                    if isinstance(source, Mapping):
                        captures.append(dict(source))
                        fetched += 1
                        bytes_fetched += int((fetched_result.metadata or {}).get("bytes") or 0)
        return (
            captures,
            errors,
            {
                "rounds": rounds_run,
                "queries": queries,
                "discovered": discovered,
                "fetched": fetched,
                "max_sources": max_sources,
                "bytes_fetched": bytes_fetched,
                "max_research_bytes": max_bytes,
                "attempted_queries": sorted(attempted_queries),
            },
        )

    async def _run(self, request, args, context) -> CapabilityResult:
        """Run one bounded, explicit research workflow.

        This is orchestration over the durable primitives above, not a second
        planner or a hidden inference loop.  The caller supplies requirements
        (or the objective deterministically becomes one requirement), chooses
        the source captures to attempt, and supplies exact extraction excerpts.
        Every source/evidence operation remains task-scoped and the outer
        ``research:run`` dispatch declares the complete effect envelope.
        """
        if not request.task_id:
            return _result(request, ok=False, error="run requires a task")
        objective = str(args.get("objective") or "").strip()
        if not objective:
            return _result(request, ok=False, error="run requires objective")

        raw_requirements = args.get("requirements")
        if not isinstance(raw_requirements, list) or not raw_requirements:
            queries = _strings(args.get("queries"), limit=10)
            raw_requirements = [
                {
                    "id": "objective",
                    "claim_id": "research-objective",
                    "question": objective,
                    "queries": queries or [objective],
                }
            ]

        plan_result = await self._plan(
            request,
            {**args, "objective": objective, "requirements": raw_requirements},
            context,
        )
        if plan_result.status is not CapabilityResultStatus.OK:
            return _result(request, ok=False, error=plan_result.error or "research plan failed")
        plan = _decode_object(plan_result.output)
        gap_ids = [
            str(item["gap"]["id"])
            for item in plan.get("requirements", [])
            if isinstance(item, Mapping)
            and isinstance(item.get("gap"), Mapping)
            and item["gap"].get("id")
        ]

        captures: list[dict[str, Any]] = []
        capture_errors: list[dict[str, Any]] = []
        acquisition: dict[str, Any] | None = None
        if bool(args.get("autonomous")):
            auto_captures, auto_errors, acquisition = await self._autonomous_acquire(
                request,
                context,
                [item for item in raw_requirements if isinstance(item, Mapping)],
                args,
            )
            captures.extend(auto_captures)
            capture_errors.extend(auto_errors)
        source_specs = args.get("source_specs")
        if isinstance(source_specs, list):
            for index, raw_spec in enumerate(source_specs[:10]):
                if not isinstance(raw_spec, Mapping):
                    capture_errors.append(
                        {"index": index, "error": "source spec must be an object"}
                    )
                    continue
                spec = dict(raw_spec)
                source_result = (
                    await self._record_source(request, spec, context)
                    if "content" in spec or "artifact_uri" in spec
                    else await self._fetch(request, spec, context)
                )
                if source_result.status is not CapabilityResultStatus.OK:
                    capture_errors.append(
                        {
                            "index": index,
                            "uri": spec.get("uri"),
                            "error": source_result.error or "source capture failed",
                        }
                    )
                    continue
                payload = _decode_object(source_result.output)
                source = payload.get("source")
                if isinstance(source, Mapping):
                    captures.append(dict(source))

        # Search after capture as well as during planning, so the response
        # reports the corpus that actually exists at the end of this run.
        search_results: list[dict[str, Any]] = []
        search_errors: list[dict[str, Any]] = []
        search_queries = _strings(args.get("queries"), limit=10)
        for item in raw_requirements:
            if isinstance(item, Mapping):
                search_queries.extend(_strings(item.get("queries"), limit=5))
        for query in _unique_strings(search_queries):
            search_result = await self._search(
                request, {"query": query, "limit": int(args.get("limit") or 50)}, context
            )
            if search_result.status is not CapabilityResultStatus.OK:
                search_errors.append(
                    {"query": query, "error": search_result.error or "search failed"}
                )
                continue
            search_results.append({"query": query, **_decode_object(search_result.output)})

        sources_by_id: dict[str, Mapping[str, Any]] = {
            str(source["id"]): source for source in captures if source.get("id")
        }
        for source in await self._store.list_sources(
            task_id=request.task_id,
            project_id=getattr(getattr(context, "workspace", None), "id", None),
            limit=200,
        ):
            sources_by_id.setdefault(source.id, source.to_record())
        sources_by_uri = {
            str(source.get("canonical_uri")): source
            for source in sources_by_id.values()
            if source.get("canonical_uri")
        }

        evidence_records: list[dict[str, Any]] = []
        evidence_errors: list[dict[str, Any]] = []
        extractions = args.get("extractions")
        if isinstance(extractions, list):
            for index, raw_extraction in enumerate(extractions[:100]):
                if not isinstance(raw_extraction, Mapping):
                    evidence_errors.append(
                        {"index": index, "error": "extraction must be an object"}
                    )
                    continue
                extraction = dict(raw_extraction)
                source_id = str(extraction.get("source_id") or "")
                if not source_id and extraction.get("uri"):
                    try:
                        source_id = str(
                            sources_by_uri[canonicalize_uri(str(extraction["uri"]))]["id"]
                        )
                    except (KeyError, SourcePolicyError):
                        source_id = ""
                evidence_args = {
                    key: value
                    for key, value in extraction.items()
                    if key not in {"source_id", "uri"}
                }
                evidence_args.update({"source_id": source_id, "operation": "record_evidence"})
                evidence_result = await self._record_evidence(request, evidence_args, context)
                if evidence_result.status is not CapabilityResultStatus.OK:
                    evidence_errors.append(
                        {
                            "index": index,
                            "source_id": source_id,
                            "error": evidence_result.error or "evidence recording failed",
                        }
                    )
                    continue
                evidence = _decode_object(evidence_result.output).get("evidence")
                if isinstance(evidence, Mapping):
                    evidence_records.append(dict(evidence))

        assessed_result = await self._assess(
            request,
            {"gap_ids": gap_ids},
            context,
        )
        assessed = (
            _decode_object(assessed_result.output)
            if assessed_result.status is CapabilityResultStatus.OK
            else {
                "ready": False,
                "error": assessed_result.error or "research assessment failed",
            }
        )
        bundle_result = await self._bundle(request, {"limit": args.get("limit")}, context)
        bundle = (
            _decode_object(bundle_result.output)
            if bundle_result.status is CapabilityResultStatus.OK
            else {
                "ready": False,
                "error": bundle_result.error or "research bundle failed",
            }
        )
        ready = bool(bundle.get("ready")) and not (
            capture_errors or search_errors or evidence_errors
        )
        required_open_gaps = bundle.get("required_open_gaps")
        if not isinstance(required_open_gaps, (list, tuple)):
            required_open_gaps = ()
        unverified_closed_gaps = bundle.get("unverified_closed_gaps")
        if not isinstance(unverified_closed_gaps, (list, tuple)):
            unverified_closed_gaps = ()
        gaps_raw = bundle.get("gaps")
        gaps: list[Mapping[str, Any]] = (
            [item for item in gaps_raw if isinstance(item, Mapping)]
            if isinstance(gaps_raw, list)
            else []
        )
        research_completion = {
            "ready": ready,
            "bundle_id": hashlib.sha256(
                json.dumps(
                    {"task_id": request.task_id, "objective": objective, "gap_ids": gap_ids},
                    sort_keys=True,
                ).encode()
            ).hexdigest()[:32],
            "requirement_ids": [
                str((item.get("gap") or {}).get("metadata", {}).get("requirement_id"))
                for item in plan.get("requirements", [])
                if isinstance(item, Mapping)
                and isinstance(item.get("gap"), Mapping)
                and (item.get("gap") or {}).get("metadata", {}).get("requirement_id")
            ],
            "closed_gap_ids": [
                str((item or {}).get("id")) for item in gaps if item.get("status") == "CLOSED"
            ],
            "evidence_ids": [str(item.get("id")) for item in evidence_records if item.get("id")],
            "required_open_gaps": list(required_open_gaps),
            "unverified_closed_gaps": list(unverified_closed_gaps),
            "bundle_ready": bool(bundle.get("ready")),
        }
        return _result(
            request,
            output=_json(
                {
                    "workflow": "bounded-research",
                    "objective": objective,
                    "plan": plan,
                    "captures": captures,
                    "capture_errors": capture_errors,
                    "autonomous_acquisition": acquisition,
                    "search": search_results,
                    "search_errors": search_errors,
                    "evidence": evidence_records,
                    "evidence_errors": evidence_errors,
                    "assessment": assessed,
                    "bundle": bundle,
                    "ready": ready,
                }
            ),
            metadata={"research_completion": research_completion},
        )

    async def _verify_evidence(
        self,
        evidence: EvidenceObject,
        source: SourceRecord,
    ) -> dict[str, Any]:
        if not source.artifact_uri or self._artifacts is None:
            return {"status": "unverified", "reason": "source snapshot not captured"}
        content = await self._artifacts.load(source.artifact_uri)
        content_hash = hashlib.sha256(content).hexdigest()
        hash_matches = not source.content_hash or content_hash == source.content_hash
        found = hash_matches and evidence.exact_supporting_excerpt.encode("utf-8") in content
        result: dict[str, Any] = {
            "status": "verified" if found else "invalid",
            "content_hash": content_hash,
            "hash_matches": hash_matches,
        }
        if not found:
            return result

        # Structured evidence is still evidence only when its receipt is
        # internally coherent.  The source hash/excerpt check above proves
        # the captured bytes; these checks prove that a measurement or
        # execution claim has the fields needed for replay/review.
        if evidence.evidence_type in {
            "execution",
            "measurement",
            "observation",
            "derivation",
        }:
            receipt = evidence.metadata.get("receipt")
            structured = _verify_receipt(evidence.evidence_type, receipt)
            result["receipt"] = structured
            if structured["status"] != "verified":
                result["status"] = "invalid"
        return result


def _verify_receipt(evidence_type: str, receipt: Any) -> dict[str, Any]:
    """Validate the minimum replay boundary for structured evidence."""
    if not isinstance(receipt, Mapping):
        return {"status": "unverified", "reason": "structured receipt is missing"}
    required: dict[str, tuple[str, ...]] = {
        "execution": ("capability_id", "input_hash", "environment_fingerprint"),
        "measurement": ("value", "unit", "observed_at", "environment_fingerprint"),
        "observation": ("observation", "observed_at", "environment_fingerprint"),
        "derivation": ("inputs", "derivation", "environment_fingerprint"),
    }
    missing = [
        field
        for field in required.get(evidence_type, ())
        if field not in receipt or receipt[field] in (None, "", [])
    ]
    if missing:
        return {"status": "invalid", "missing": missing}
    if evidence_type == "execution":
        exit_code = receipt.get("exit_code")
        ok = receipt.get("ok")
        status = str(receipt.get("status") or "").casefold()
        if (
            exit_code not in (None, 0)
            or ok is False
            or status
            in {
                "failed",
                "error",
                "cancelled",
            }
        ):
            return {
                "status": "invalid",
                "reason": "execution receipt does not show a successful exit",
            }
    return {
        "status": "verified",
        "evidence_type": evidence_type,
        "fields": sorted(str(key) for key in receipt),
    }


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _strings(value: Any, *, limit: int) -> list[str]:
    """Return bounded, non-empty strings from a schema-validated list."""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value[:limit] if str(item).strip()]


def _unique_strings(values: list[str]) -> list[str]:
    """Deduplicate bounded workflow queries without changing their order."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _decode_object(value: str) -> dict[str, Any]:
    """Decode an internal capability response without trusting its shape."""
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _unique_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate local search hits while preserving query/rank order."""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        source = candidate.get("source") if isinstance(candidate, Mapping) else None
        source_id = str(source.get("id") or "") if isinstance(source, Mapping) else ""
        key = source_id or json.dumps(candidate, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


async def _index_snapshot(
    store,
    source: SourceRecord,
    snapshot: bytes | str | None,
    *,
    mime_type: str,
) -> None:
    """Populate the durable lexical projection when the store supports it."""
    index = getattr(store, "index_content", None)
    if index is None or snapshot is None:
        return
    if isinstance(snapshot, bytes):
        media = str(mime_type or "").lower()
        if not (
            media.startswith("text/")
            or media.endswith(("+json", "+xml"))
            or media
            in {
                "application/json",
                "application/xml",
                "application/javascript",
            }
        ):
            return
        text = snapshot.decode("utf-8", errors="replace")
        content_hash = hashlib.sha256(snapshot).hexdigest()
    else:
        text = str(snapshot)
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    await index(
        source.id,
        text,
        content_hash=content_hash,
        mime_type=str(mime_type or ""),
    )


def _result(
    request,
    *,
    ok: bool = True,
    output: str = "",
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
        metadata=dict(metadata or {}),
    )


def _source_visible(source: SourceRecord, request: CapabilityRequest, context: Any) -> bool:
    """Return whether a source belongs to this task or its project overlay."""
    if source.task_id == request.task_id:
        return True
    project_id = getattr(getattr(context, "workspace", None), "id", None)
    return source.task_id is None and bool(project_id) and source.project_id == project_id


async def _evidence_visible(
    evidence: EvidenceObject,
    request: CapabilityRequest,
    context: Any,
    source_lookup,
) -> bool:
    """Apply task/project visibility to an evidence object and its source."""
    if evidence.task_id not in (None, request.task_id):
        return False
    source = await source_lookup(evidence.source_id)
    return source is not None and _source_visible(source, request, context)


async def _artifact_visible(artifacts: Any, uri: str, task_id: str) -> bool:
    """Artifact URIs are references, not bearer credentials."""
    refs = await artifacts.list(task_id=task_id, limit=1000)
    return any(getattr(ref, "uri", None) == uri for ref in refs)


__all__ = [
    "BraveSearchProvider",
    "HttpDiscoveryProvider",
    "ResearchDiscoveryProvider",
    "ResearchCapability",
    "TavilySearchProvider",
]
