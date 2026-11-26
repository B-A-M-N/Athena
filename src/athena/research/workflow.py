"""Bounded autonomous acquisition and one explicit research workflow.

This collaborator deliberately performs finite policy-controlled discovery and
acquisition, then composes durable primitives.  It is not a reasoning
authority: callers and the kernel own task strategy and completion decisions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from athena.protocol.capabilities import CapabilityResult, CapabilityResultStatus
from athena.research.policy import canonicalize_uri, SourcePolicyError
from athena.research.result_codec import (
    decode_object as _decode_object,
    json_text as _json,
    result as _result,
    strings as _strings,
    unique_strings as _unique_strings,
)


async def autonomous_acquire(
    service: Any,
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
            gaps = await service._store.list_gaps(task_id=request.task_id, limit=200)
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
            if round_number + 1 < max_rounds and service._utility_inference is not None:
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
                    inferred = service._utility_inference(
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
            round_queries = [query for query in parsed_queries if query not in attempted_queries]
        for query in round_queries:
            attempted_queries.add(query)
            discovered_result = await service._discover(
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
                fetched_result = await service._fetch(
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


async def run_bounded_research(service: Any, request, args, context) -> CapabilityResult:
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

    plan_result = await service._plan(
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
        auto_captures, auto_errors, acquisition = await service._autonomous_acquire(
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
                capture_errors.append({"index": index, "error": "source spec must be an object"})
                continue
            spec = dict(raw_spec)
            source_result = (
                await service._record_source(request, spec, context)
                if "content" in spec or "artifact_uri" in spec
                else await service._fetch(request, spec, context)
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
        search_result = await service._search(
            request, {"query": query, "limit": int(args.get("limit") or 50)}, context
        )
        if search_result.status is not CapabilityResultStatus.OK:
            search_errors.append({"query": query, "error": search_result.error or "search failed"})
            continue
        search_results.append({"query": query, **_decode_object(search_result.output)})

    sources_by_id: dict[str, Mapping[str, Any]] = {
        str(source["id"]): source for source in captures if source.get("id")
    }
    for source in await service._store.list_sources(
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
                evidence_errors.append({"index": index, "error": "extraction must be an object"})
                continue
            extraction = dict(raw_extraction)
            source_id = str(extraction.get("source_id") or "")
            if not source_id and extraction.get("uri"):
                try:
                    source_id = str(sources_by_uri[canonicalize_uri(str(extraction["uri"]))]["id"])
                except (KeyError, SourcePolicyError):
                    source_id = ""
            evidence_args = {
                key: value for key, value in extraction.items() if key not in {"source_id", "uri"}
            }
            evidence_args.update({"source_id": source_id, "operation": "record_evidence"})
            evidence_result = await service._record_evidence(request, evidence_args, context)
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

    assessed_result = await service._assess(
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
    bundle_result = await service._bundle(request, {"limit": args.get("limit")}, context)
    bundle = (
        _decode_object(bundle_result.output)
        if bundle_result.status is CapabilityResultStatus.OK
        else {
            "ready": False,
            "error": bundle_result.error or "research bundle failed",
        }
    )
    ready = bool(bundle.get("ready")) and not (capture_errors or search_errors or evidence_errors)
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
