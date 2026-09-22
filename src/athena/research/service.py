"""Durable research/evidence capability.

This capability deliberately stops at the epistemic boundary.  It records
source snapshots, evidence, claim links, and research gaps, and can verify an
excerpt against a captured artifact.  Network acquisition is a single named,
policy-controlled primitive (`research.acquisition.fetch_pinned`): DNS-pinned,
redirect-free, and bounded.  No other path in this module fetches URLs.
"""

from __future__ import annotations
import hashlib
import json
import socket
from collections.abc import Mapping
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

from athena.protocol.artifacts import parse_artifact_uri
from athena.research.commands import ResearchCommand
from athena.protocol.capabilities import (
    CapabilityFailure,
    CapabilityFailureCode,
    CapabilityRequest,
    CapabilityResult,
)
from athena.research.models import (
    EvidenceBundle,
    EvidenceObject,
    ResearchGap,
    SourceRecord,
    classify_evidence_quality,
)
from athena.research.acquisition import (
    fetch_pinned,
    SourceAcquisitionError,
)
from athena.research.discovery import (
    BraveSearchProvider,
    HttpDiscoveryProvider,
    ResearchDiscoveryProvider,
    TavilySearchProvider,
)
from athena.research.policy import (
    SourcePolicy,
    classify_source,
)
from athena.research.domain_dispatch import RunCommandMixin
from athena.research.result_codec import (
    artifact_visible as _artifact_visible,
    evidence_visible as _evidence_visible,
    index_snapshot as _index_snapshot,
    json_text as _json,
    result as _result,
    source_visible as _source_visible,
    strings as _strings,
    unique_candidates as _unique_candidates,
)
from athena.research.verification import verify_evidence


class ResearchService(RunCommandMixin):
    """Coordinate discovery, durable evidence, policy, and verification."""

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

    def _conformance_failure_probe(self) -> dict:
        failure = CapabilityFailure(
            code=CapabilityFailureCode.DOMAIN_REJECTED,
            detail="conformance probe",
            stage="invoke",
        )
        return failure.to_metadata()

    async def execute(self, request: CapabilityRequest, **kw) -> CapabilityResult:
        """Capability adapter: translate one request around ``run_command()``."""

        args = dict(request.arguments or {})
        context = kw.get("context")
        project_id = getattr(getattr(context, "workspace", None), "id", None)
        command = ResearchCommand(
            operation=str(args.get("operation") or ""),
            arguments=args,
            task_id=request.task_id,
            session_id=request.session_id,
            call_id=request.call_id,
            project_id=project_id,
        )
        domain = await self.run_command(command)
        if not domain.ok:
            return CapabilityResult.failure(
                request,
                CapabilityFailure(
                    code=CapabilityFailureCode.DOMAIN_REJECTED,
                    detail=domain.error or "research command failed",
                    stage="invoke",
                ),
            )
        payload = dict(domain.payload)
        raw_output = payload.pop("_raw_output", None)
        if raw_output is None:
            raw_output = json.dumps(payload)
        return _result(request, output=str(raw_output))

    async def invoke(self, request: CapabilityRequest, **kw) -> CapabilityResult:
        """Compatibility entry point for direct domain-service callers."""
        return await self.execute(request, **kw)

    async def _discover(self, request, args, context) -> CapabilityResult:
        """Compose bounded discovery through its extracted collaborator."""
        from athena.research.discovery_candidates import discover_candidates

        return await discover_candidates(self, request, args, context)

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
        max_bytes = min(int(args.get("max_bytes") or 2_000_000), 10_000_000)
        try:
            acquired = await fetch_pinned(
                source_policy=self._source_policy,
                host_resolver=self._host_resolver,
                canonical=canonical,
                host=host,
                port=port,
                timeout=timeout,
                max_bytes=max_bytes,
            )
        except SourceAcquisitionError as exc:
            return _result(request, ok=False, error=str(exc))

        content = acquired.content
        media_type = acquired.media_type
        status_code = acquired.status_code
        resolved_addresses = acquired.resolved_addresses
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
        excerpt = str(args.get("excerpt") or "")
        # The model may propose the excerpt, but the immutable source snapshot
        # remains the authority.  Persist enough provenance to make that
        # later verification auditable without trusting the proposal itself.
        metadata.setdefault("source_content_hash", source.content_hash)
        metadata.setdefault("excerpt_hash", hashlib.sha256(excerpt.encode("utf-8")).hexdigest())
        metadata.setdefault("normalization_version", "1")
        evidence = EvidenceObject.for_content(
            source_id=source_id,
            extracted_claim=str(args.get("claim") or ""),
            exact_supporting_excerpt=excerpt,
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
            source_revision=source.revision,
            source_content_hash=source.content_hash,
            acquired_at=source.retrieved_at,
            confidence_calibration=args.get("confidence_calibration") or {},
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
                check["evidence_id"]
                for check in checks
                if check.get("status") == "verified" and check.get("quality") == "supported"
            ]
            can_close = (
                bool(candidates)
                and bool(verified)
                and not conflicts
                and all(
                    check.get("status") == "verified" and check.get("quality") == "supported"
                    for check in checks
                )
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

    async def _critique(self, request, args, context) -> CapabilityResult:
        """Run bounded, deterministic evidence-quality critique questions."""
        if not request.task_id:
            return _result(request, ok=False, error="critique requires a task")
        workspace_id = getattr(getattr(context, "workspace", None), "id", None)
        evidence = await self._store.list_evidence(
            task_id=request.task_id,
            project_id=workspace_id,
            claim_id=None,
            limit=200,
        )
        wanted_claims = {str(value) for value in args.get("claim_ids") or ()}
        if wanted_claims:
            evidence = [item for item in evidence if item.claim_id in wanted_claims]
        groups: dict[str, list[str]] = {}
        source_records: dict[str, Any] = {}
        quality_counts: dict[str, int] = {}
        for item in evidence:
            source = await self._store.get_source(item.source_id)
            if source is None or not await _evidence_visible(
                item, request, context, self._store.get_source
            ):
                continue
            quality = classify_evidence_quality(item, source)
            quality_counts[quality] = quality_counts.get(quality, 0) + 1
            source_records[source.id] = source
            metadata = dict(source.metadata)
            group = str(
                metadata.get("independence_group")
                or metadata.get("source_family")
                or metadata.get("canonical_domain")
                or source.canonical_uri
            )
            groups.setdefault(group, []).append(item.id)
        # Report the evidence objects that make a contradiction claim.  The
        # related IDs are useful edges, but returning only those targets would
        # misidentify which source asserted the conflict.
        contradictions = sorted(item.id for item in evidence if item.contradicts)
        min_groups = max(1, min(int(args.get("min_independent_groups") or 2), 10))
        independent_groups = sorted(groups)
        primary_sources = [
            source.id
            for source in source_records.values()
            if str(source.metadata.get("primary_secondary") or "").casefold() == "primary"
            or source.authority_class in {"primary", "official"}
        ]
        return _result(
            request,
            output=_json(
                {
                    "evidence_count": len(evidence),
                    "independent_evidence_groups": independent_groups,
                    "independent_group_count": len(independent_groups),
                    "meets_independence_threshold": len(independent_groups) >= min_groups,
                    "single_group_warning": len(independent_groups) <= 1 and bool(evidence),
                    "contradiction_evidence_ids": contradictions,
                    "primary_source_candidates": sorted(primary_sources),
                    "quality_counts": quality_counts,
                    "quality_questions": [
                        question
                        for quality, question in (
                            (
                                "no_evidence",
                                "Which claims still lack a captured source artifact?",
                            ),
                            (
                                "weak",
                                "Which low-confidence evidence needs calibration or corroboration?",
                            ),
                            (
                                "stale",
                                "Which evidence must be refreshed against the current source revision?",
                            ),
                            (
                                "contradicted",
                                "Which contradicted evidence must be resolved before synthesis?",
                            ),
                        )
                        if quality_counts.get(quality, 0)
                    ],
                    "questions": [
                        "What evidence would falsify this claim?",
                        "Which contradiction materially changes the answer?",
                        "Can a primary source replace this secondary source?",
                    ],
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
        quality_counts: dict[str, int] = {}
        for item in evidence:
            source = await self._store.get_source(item.source_id)
            quality = classify_evidence_quality(item, source)
            quality_counts[quality] = quality_counts.get(quality, 0) + 1
        for gap in gaps:
            if gap.status != "CLOSED" or not gap.required:
                continue
            if not gap.evidence_ids:
                unverified_closed.append(gap.id)
                continue
            for evidence_id in gap.evidence_ids:
                item = await self._store.get_evidence(evidence_id)
                source = await self._store.get_source(item.source_id) if item else None
                verification = (
                    await self._verify_evidence(item, source)
                    if item is not None
                    else {"status": "unverified", "quality": "no_evidence"}
                )
                if (
                    verification.get("status") != "verified"
                    or verification.get("quality") != "supported"
                ):
                    unverified_closed.append(gap.id)
                    break
        source_records = [source.to_record() for source in sources]
        evidence_records = [item.to_record() for item in evidence]
        gap_records = [gap.to_record() for gap in gaps]
        groups = sorted(
            {
                str(
                    source.metadata.get("independence_group")
                    or source.metadata.get("source_family")
                    or source.canonical_uri
                )
                for source in sources
            }
        )
        contradictions = sorted({related for item in evidence for related in item.contradicts})
        bundle = EvidenceBundle.create(
            request.task_id,
            ready=not required_open and not unverified_closed,
            sources=source_records,
            evidence=evidence_records,
            gaps=gap_records,
            required_open_gaps=required_open,
            unverified_closed_gaps=unverified_closed,
            independence_groups=groups,
            contradiction_evidence_ids=contradictions,
            evidence_quality_counts=quality_counts,
        )
        return _result(request, output=_json(bundle.to_record()))

    async def _autonomous_acquire(
        self,
        request,
        context,
        requirements: Sequence[Mapping[str, Any]],
        args: Mapping[str, Any],
    ):
        from athena.research.workflow import autonomous_acquire

        return await autonomous_acquire(self, request, context, requirements, args)

    async def _run(self, request, args, context) -> CapabilityResult:
        """Run one bounded, explicit research workflow through its owner."""
        from athena.research.workflow import run_bounded_research

        return await run_bounded_research(self, request, args, context)

    async def _verify_evidence(
        self,
        evidence: EvidenceObject,
        source: SourceRecord | None,
    ) -> dict[str, Any]:
        return await verify_evidence(evidence, source, self._artifacts)


__all__ = [
    "BraveSearchProvider",
    "HttpDiscoveryProvider",
    "ResearchDiscoveryProvider",
    "ResearchService",
    "TavilySearchProvider",
]
