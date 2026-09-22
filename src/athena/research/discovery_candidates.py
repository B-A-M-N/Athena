"""Research candidate discovery.

The service owns durable acquisition and evidence admission; this collaborator
collects bounded local and configured-provider candidates. Every external
candidate still passes ``SourcePolicy`` before it can become model-visible.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from athena.protocol.capabilities import CapabilityResult
from athena.research.policy import SourcePolicyError
from athena.research.result_codec import (
    json_text as _json,
    result as _result,
)


async def discover_candidates(service: Any, request, args, context) -> CapabilityResult:
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

    for hit in await service._search_content(
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

    list_sources = getattr(service._store, "list_sources", None)
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
    if service._discovery_providers:
        network_policy = getattr(workspace, "network_policy", None)
        if getattr(network_policy, "value", network_policy) == "deny":
            return _result(request, ok=False, error="network denied by workspace policy")
        for provider in service._discovery_providers:
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
                    canonical = service._source_policy.check(uri)
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
                "provider": "configured" if service._discovery_providers else "local_corpus",
                "providers": provider_status,
                "network_used": bool(service._discovery_providers),
                "rejected": rejected[:limit],
            }
        ),
    )
