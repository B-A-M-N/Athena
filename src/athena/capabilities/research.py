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
from typing import Any
from urllib.parse import urlsplit

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
from athena.research.policy import SourcePolicy, SourcePolicyError, classify_source

_SOURCE_TYPES = ("web", "paper", "documentation", "dataset", "code", "local")
_EVIDENCE_TYPES = ("quote", "measurement", "observation", "derivation", "execution")
_GAP_KINDS = (
    "unsupported_claim", "conflict", "stale_source", "source_quality",
    "unanswered_question",
)


class ResearchCapability:
    """Model-visible access to the durable Evidence/Research Fabric."""

    descriptor = CapabilityDescriptor(
        id="research",
        description=(
            "Durable evidence-backed research records: record and list source "
            "snapshots, extract exact evidence, link evidence to Athena claims, "
            "track research gaps, search the local corpus, and verify excerpts. "
            "External fetching is a separate allowlisted operation."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {"type": "string", "enum": [
                    "fetch", "record_source", "sources", "search", "record_evidence", "evidence",
                    "record_gap", "gaps", "close_gap", "verify",
                ]},
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
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "corroborates": {"type": "array", "items": {"type": "string"}},
                "contradicts": {"type": "array", "items": {"type": "string"}},
                "objective": {"type": "string", "minLength": 1, "maxLength": 20_000},
                "question": {"type": "string", "minLength": 1, "maxLength": 20_000},
                "kind": {"type": "string", "enum": list(_GAP_KINDS)},
                "required": {"type": "boolean"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "query": {"type": "string", "maxLength": 2000},
                "status": {"type": "string", "enum": ["OPEN", "CLOSED"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 30},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 10_000_000},
                "metadata": {"type": "object", "additionalProperties": True},
            },
            "additionalProperties": False,
        },
        effects=frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, store, *, artifact_store=None,
                 source_policy: SourcePolicy | None = None,
                 host_resolver=None) -> None:
        self._store = store
        self._artifacts = artifact_store
        self._source_policy = source_policy or SourcePolicy()
        self._host_resolver = host_resolver or socket.getaddrinfo

    async def invoke(self, request: CapabilityRequest, **kw) -> CapabilityResult:
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")
        context = kw.get("context")
        if self._store is None:
            return _result(request, ok=False, error="research store not available")
        try:
            if operation == "fetch":
                return await self._fetch(request, args, context)
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
                return await self._close_gap(request, args)
            if operation == "verify":
                return await self._verify(request, args, context)
            return _result(request, ok=False, error=f"unknown operation: {operation}")
        except (KeyError, SourcePolicyError, ValueError) as exc:
            return _result(request, ok=False, error=str(exc))
        except (OSError, RuntimeError, TypeError, sqlite3.Error) as exc:
            # Capability failures are model-visible, but unexpected storage
            # errors remain explicit rather than becoming false evidence.
            return _result(request, ok=False, error=f"research operation failed: {exc}")

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
                request, ok=False,
                error="fetch accepts http/https; use record_source for artifacts",
            )
        network_policy = getattr(
            getattr(context, "workspace", None), "network_policy", None
        )
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
                lambda: self._host_resolver(
                    host, port, type=socket.SOCK_STREAM),
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
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": "Athena-Research/1"},
            ) as client, client.stream("GET", canonical) as response:
                    if response.status_code >= 300:
                        location = response.headers.get("location")
                        suffix = f" location={location}" if location else ""
                        return _result(
                            request, ok=False,
                            error=f"source fetch returned HTTP {response.status_code}{suffix}",
                            metadata={"status_code": response.status_code},
                        )
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            return _result(
                                request, ok=False,
                                error=f"source exceeds max_bytes={max_bytes}",
                                metadata={"status_code": response.status_code},
                            )
                        chunks.append(chunk)
                    media_type = response.headers.get(
                        "content-type", "application/octet-stream"
                    ).split(";", 1)[0].strip() or "application/octet-stream"
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
                request, ok=False,
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
            task_id=request.task_id, project_id=workspace_id,
            query=query, limit=int(args.get("limit") or 50),
        )
        evidence = await self._store.list_evidence(
            task_id=request.task_id, project_id=workspace_id,
            query=query, limit=int(args.get("limit") or 50),
        )
        content_hits = await self._search_content(
            query,
            task_id=request.task_id,
            project_id=workspace_id,
            limit=int(args.get("limit") or 50),
        )
        return _result(request, output=_json({
            "query": query,
            "sources": [source.to_record() for source in sources],
            "evidence": [item.to_record() for item in evidence],
            "content_hits": content_hits,
        }))

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
        related_ids = tuple(args.get("corroborates") or ()) + tuple(
            args.get("contradicts") or ()
        )
        for related_id in related_ids:
            related = await self._store.get_evidence(str(related_id))
            if related is None or not await _evidence_visible(
                related, request, context, self._store.get_source
            ):
                return _result(
                    request, ok=False,
                    error=f"related evidence is not visible: {related_id}",
                )
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
            metadata=args.get("metadata") or {},
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
            str(args.get("objective") or ""), str(args.get("question") or ""),
            kind=str(args.get("kind") or "unsupported_claim"),
            required=bool(args.get("required", True)), task_id=request.task_id,
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

    async def _close_gap(self, request, args) -> CapabilityResult:
        if not request.task_id:
            return _result(request, ok=False, error="close_gap requires a task")
        gap_id = str(args.get("gap_id") or "")
        if not gap_id:
            return _result(request, ok=False, error="close_gap requires gap_id")
        gap = await self._store.close_gap(
            gap_id, evidence_ids=tuple(args.get("evidence_ids") or ()),
            task_id=request.task_id)
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
        if not source.artifact_uri or self._artifacts is None:
            return _result(request, output=_json({
                "status": "unverified", "reason": "source snapshot not captured",
                "evidence_id": evidence.id,
            }))
        content = await self._artifacts.load(source.artifact_uri)
        content_hash = hashlib.sha256(content).hexdigest()
        hash_matches = not source.content_hash or content_hash == source.content_hash
        found = hash_matches and (
            evidence.exact_supporting_excerpt.encode("utf-8") in content
        )
        return _result(request, output=_json({
            "status": "verified" if found else "invalid",
            "evidence_id": evidence.id,
            "source_id": source.id,
            "content_hash": content_hash,
            "hash_matches": hash_matches,
        }))


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


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
            or media in {
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


def _result(request, *, ok: bool = True, output: str = "",
            error: str | None = None, metadata: dict[str, Any] | None = None):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
        metadata=dict(metadata or {}),
    )


def _source_visible(source: SourceRecord, request: CapabilityRequest,
                    context: Any) -> bool:
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


__all__ = ["ResearchCapability"]
