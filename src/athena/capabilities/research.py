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
from collections.abc import Mapping
from typing import Any
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
                "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 30},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 10_000_000},
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
    ) -> None:
        self._store = store
        self._artifacts = artifact_store
        self._source_policy = source_policy or SourcePolicy()
        self._host_resolver = host_resolver or socket.getaddrinfo
        self._discovery_provider = discovery_provider

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
            key = source_id or uri
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
                key = source_id or uri
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

        provider = self._discovery_provider
        rejected: list[dict[str, str]] = []
        if provider is not None and len(candidates) < limit:
            network_policy = getattr(workspace, "network_policy", None)
            if getattr(network_policy, "value", network_policy) == "deny":
                return _result(request, ok=False, error="network denied by workspace policy")
            provided = provider(
                query=query,
                limit=limit,
                task_id=request.task_id,
                project_id=project_id,
                context=context,
            )
            if asyncio.iscoroutine(provided):
                provided = await provided
            if not isinstance(provided, list):
                return _result(request, ok=False, error="discovery provider returned a non-list")
            for item in provided[:limit]:
                if not isinstance(item, Mapping):
                    rejected.append({"reason": "candidate is not an object"})
                    continue
                uri = str(item.get("uri") or item.get("url") or "")
                try:
                    canonical = self._source_policy.check(uri)
                except SourcePolicyError as exc:
                    rejected.append({"uri": uri[:4096], "reason": str(exc)})
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
                        "origin": "configured_provider",
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
                    "provider": "configured" if provider is not None else "local_corpus",
                    "network_used": provider is not None,
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
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.run_in_executor(
                None,
                lambda: self._host_resolver(host, port, type=socket.SOCK_STREAM),
            )
            addresses = [str(info[4][0]) for info in infos]
            resolved_addresses = self._source_policy.check_resolved(host, addresses)
        except (OSError, SourcePolicyError) as exc:
            return _result(request, ok=False, error=f"source DNS check failed: {exc}")

        import httpx

        timeout = min(float(args.get("timeout") or 15.0), 30.0)
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
        return _result(
            request,
            output=_json(
                {
                    "workflow": "bounded-research",
                    "objective": objective,
                    "plan": plan,
                    "captures": captures,
                    "capture_errors": capture_errors,
                    "search": search_results,
                    "search_errors": search_errors,
                    "evidence": evidence_records,
                    "evidence_errors": evidence_errors,
                    "assessment": assessed,
                    "bundle": bundle,
                    "ready": ready,
                }
            ),
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


__all__ = ["ResearchCapability"]
