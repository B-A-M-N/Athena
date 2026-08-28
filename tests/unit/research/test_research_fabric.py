from __future__ import annotations

import json
import hashlib
import socket
from types import SimpleNamespace
from typing import ClassVar

import pytest

from athena.capabilities.research import ResearchCapability
from athena.protocol.artifacts import ArtifactRef
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityResultStatus,
    EffectClass,
)
from athena.research.models import EvidenceObject, SourceRecord
from athena.research.policy import SourcePolicy, SourcePolicyError, canonicalize_uri


def test_source_policy_requires_explicit_external_allowlist():
    with pytest.raises(SourcePolicyError):
        SourcePolicy().check("https://docs.example.test/guide#intro")
    with pytest.raises(SourcePolicyError, match="credentials"):
        SourcePolicy(allowed_domains=("example.test",)).check(
            "https://user:secret@example.test/guide"
        )

    policy = SourcePolicy(allowed_domains=("example.test",))
    assert policy.check("HTTPS://docs.example.test:443/guide#intro") == (
        "https://docs.example.test/guide"
    )
    with pytest.raises(SourcePolicyError):
        policy.check("https://other.test/guide")


def test_source_identity_changes_when_snapshot_changes():
    first = SourceRecord.for_uri(
        canonicalize_uri("https://example.test/doc"), content_hash="a")
    second = SourceRecord.for_uri(
        canonicalize_uri("https://example.test/doc"), content_hash="b")
    assert first.id != second.id


def test_source_policy_rejects_private_dns_result():
    policy = SourcePolicy(allowed_domains=("example.test",))
    with pytest.raises(SourcePolicyError, match="private/local"):
        policy.check_resolved("docs.example.test", ("127.0.0.1",))
    assert policy.check_resolved("docs.example.test", ("93.184.216.34",)) == (
        "93.184.216.34",
    )


class _MemoryResearchStore:
    def __init__(self):
        self.sources = {}
        self.evidence = {}
        self.gaps = {}
        self.content = {}

    async def save_source(self, source):
        self.sources[source.id] = source
        return source

    async def get_source(self, source_id):
        return self.sources.get(source_id)

    async def list_sources(self, **kwargs):
        return list(self.sources.values())

    async def index_content(self, source_id, content, **kwargs):
        self.content[source_id] = str(content)

    async def search_content(self, query, *, task_id=None, project_id=None, limit=50, **kwargs):
        terms = [term.casefold() for term in str(query).split() if term.strip()]
        hits = []
        for source in self.sources.values():
            if task_id is not None and source.task_id != task_id:
                continue
            content = self.content.get(source.id, "")
            if terms and all(term in content.casefold() for term in terms):
                hits.append({
                    "source": source.to_record(),
                    "snippet": content,
                    "indexed_content_hash": source.content_hash,
                    "mime_type": "text/plain",
                })
        return hits[:limit]

    async def save_evidence(self, evidence):
        self.evidence[evidence.id] = evidence
        return evidence

    async def get_evidence(self, evidence_id):
        return self.evidence.get(evidence_id)

    async def list_evidence(self, **kwargs):
        return list(self.evidence.values())

    async def save_gap(self, gap):
        self.gaps[gap.id] = gap
        return gap

    async def list_gaps(self, **kwargs):
        return list(self.gaps.values())

    async def close_gap(self, gap_id, *, evidence_ids=(), task_id=None):
        gap = self.gaps.get(gap_id)
        if gap is None or (task_id is not None and gap.task_id != task_id):
            return None
        from athena.research.models import ResearchGap
        updated = ResearchGap(
            **{**gap.to_record(), "status": "CLOSED", "evidence_ids": list(evidence_ids)}
        )
        self.gaps[gap_id] = updated
        return updated


class _MemoryArtifacts:
    def __init__(self):
        self.data = {}
        self.refs = []

    async def save(self, **kwargs):
        content = kwargs["content"]
        if isinstance(content, str):
            content = content.encode()
        uri = "artifact://sha256/source"
        self.data[uri] = content
        ref = ArtifactRef(
            id=uri, uri=uri, hash="source", task_id=kwargs.get("task_id")
        )
        self.refs.append(ref)
        return ref

    async def list(self, *, task_id=None, limit=100):
        return [ref for ref in self.refs if ref.task_id == task_id][:limit]

    async def load(self, ref):
        return self.data[ref]


@pytest.mark.asyncio
async def test_record_source_cannot_import_another_tasks_artifact():
    store = _MemoryResearchStore()
    artifacts = _MemoryArtifacts()
    foreign_uri = "artifact://sha256/foreign"
    artifacts.data[foreign_uri] = b"private snapshot"
    artifacts.refs.append(ArtifactRef(
        id=foreign_uri, uri=foreign_uri, hash="foreign", task_id="task-other"
    ))
    capability = ResearchCapability(
        store,
        artifact_store=artifacts,
        source_policy=SourcePolicy(allowed_domains=("example.test",)),
    )

    result = await capability.invoke(CapabilityRequest(
        capability_id="research", task_id="task-a", call_id="foreign-artifact",
        arguments={
            "operation": "record_source",
            "uri": "https://example.test/private",
            "artifact_uri": foreign_uri,
        },
    ))

    assert result.status is CapabilityResultStatus.FAILED
    assert "not visible" in (result.error or "")


@pytest.mark.asyncio
async def test_capability_records_and_verifies_evidence():
    store = _MemoryResearchStore()
    artifacts = _MemoryArtifacts()
    capability = ResearchCapability(
        store,
        artifact_store=artifacts,
        source_policy=SourcePolicy(allowed_domains=("example.test",)),
    )
    context = SimpleNamespace(workspace=SimpleNamespace(id="repo"))
    source_result = await capability.invoke(CapabilityRequest(
        capability_id="research", task_id="task-1", call_id="source-1",
        arguments={
            "operation": "record_source", "uri": "https://example.test/doc",
            "source_type": "documentation", "content": "status=ready",
        },
    ), context=context)
    assert source_result.status is CapabilityResultStatus.OK
    source_id = json.loads(source_result.output)["source"]["id"]

    evidence_result = await capability.invoke(CapabilityRequest(
        capability_id="research", task_id="task-1", call_id="evidence-1",
        arguments={
            "operation": "record_evidence", "source_id": source_id,
            "claim": "The status is ready.", "excerpt": "status=ready",
        },
    ))
    assert evidence_result.status is CapabilityResultStatus.OK
    evidence_id = json.loads(evidence_result.output)["evidence"]["id"]

    verified = await capability.invoke(CapabilityRequest(
        capability_id="research", task_id="task-1", call_id="verify-1",
        arguments={"operation": "verify", "evidence_id": evidence_id},
    ))
    assert json.loads(verified.output)["status"] == "verified"

    searched = await capability.invoke(CapabilityRequest(
        capability_id="research", task_id="task-1", call_id="search-1",
        arguments={"operation": "search", "query": "status"},
    ), context=context)
    search_payload = json.loads(searched.output)
    assert search_payload["sources"][0]["id"] == source_id
    assert search_payload["evidence"][0]["id"] == evidence_id

    discovered = await capability.invoke(CapabilityRequest(
        capability_id="research", task_id="task-1", call_id="discover-1",
        arguments={"operation": "discover", "query": "status"},
    ), context=context)
    assert discovered.status is CapabilityResultStatus.OK
    discover_payload = json.loads(discovered.output)
    assert discover_payload["provider"] == "local_corpus"
    assert discover_payload["candidates"][0]["source_id"] == source_id
    assert discover_payload["network_used"] is False


@pytest.mark.asyncio
async def test_task_cannot_cite_or_verify_another_tasks_private_evidence():
    store = _MemoryResearchStore()
    foreign_source = SourceRecord.for_uri(
        "https://example.test/private", content_hash="foreign",
        task_id="task-2",
    )
    await store.save_source(foreign_source)
    evidence = EvidenceObject.for_content(
        source_id=foreign_source.id,
        extracted_claim="private claim",
        exact_supporting_excerpt="private",
        task_id="task-2",
    )
    await store.save_evidence(evidence)
    capability = ResearchCapability(
        store, source_policy=SourcePolicy(allowed_domains=("example.test",))
    )
    context = SimpleNamespace(workspace=SimpleNamespace(id="repo"))

    cited = await capability.invoke(CapabilityRequest(
        capability_id="research", task_id="task-1", call_id="foreign-cite",
        arguments={
            "operation": "record_evidence", "source_id": foreign_source.id,
            "claim": "leak", "excerpt": "private",
        },
    ), context=context)
    assert cited.status is CapabilityResultStatus.FAILED

    verified = await capability.invoke(CapabilityRequest(
        capability_id="research", task_id="task-1", call_id="foreign-verify",
        arguments={"operation": "verify", "evidence_id": evidence.id},
    ), context=context)
    assert verified.status is CapabilityResultStatus.FAILED


@pytest.mark.asyncio
async def test_fetch_snapshots_allowlisted_source(monkeypatch):
    store = _MemoryResearchStore()
    artifacts = _MemoryArtifacts()
    capability = ResearchCapability(
        store,
        artifact_store=artifacts,
        source_policy=SourcePolicy(allowed_domains=("example.test",)),
        host_resolver=lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ],
    )

    class _Response:
        status_code = 200
        headers: ClassVar = {"content-type": "text/plain; charset=utf-8"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def aiter_bytes(self):
            yield b"status=ready"

    class _Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, method, url):
            assert method == "GET"
            assert url == "https://example.test/doc"
            return _Response()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    result = await capability.invoke(CapabilityRequest(
        capability_id="research", task_id="task-fetch", call_id="fetch-1",
        arguments={
            "operation": "fetch",
            "uri": "https://example.test/doc#section",
            "max_bytes": 100,
        },
    ))

    assert result.status is CapabilityResultStatus.OK
    source = json.loads(result.output)["source"]
    assert source["artifact_uri"] == "artifact://sha256/source"
    assert source["content_hash"]
    assert source["metadata"]["bytes"] == 12
    assert await store.get_source(source["id"]) is not None


@pytest.mark.asyncio
async def test_fetch_rejects_non_allowlisted_source_before_network(monkeypatch):
    called = False

    class _Client:
        def __init__(self, **kwargs):
            nonlocal called
            called = True

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    capability = ResearchCapability(
        _MemoryResearchStore(),
        artifact_store=_MemoryArtifacts(),
        source_policy=SourcePolicy(allowed_domains=("example.test",)),
    )
    result = await capability.invoke(CapabilityRequest(
        capability_id="research", task_id="task-fetch", call_id="fetch-2",
        arguments={"operation": "fetch", "uri": "https://other.test/doc"},
    ))

    assert result.status is CapabilityResultStatus.FAILED
    assert "allowlisted" in (result.error or "")
    assert called is False


@pytest.mark.asyncio
async def test_plan_assess_and_bundle_require_verified_evidence():
    store = _MemoryResearchStore()
    artifacts = _MemoryArtifacts()
    capability = ResearchCapability(
        store,
        artifact_store=artifacts,
        source_policy=SourcePolicy(allowed_domains=("example.test",)),
    )
    context = SimpleNamespace(workspace=SimpleNamespace(id="repo"))

    plan = await capability.invoke(CapabilityRequest(
        capability_id="research", task_id="task-plan", call_id="plan-1",
        arguments={
            "operation": "plan",
            "objective": "verify the release status",
            "requirements": [{
                "id": "release-status",
                "claim_id": "claim-release-status",
                "question": "Is the release ready?",
                "queries": ["status=ready"],
            }],
        },
    ), context=context)
    plan_payload = json.loads(plan.output)
    gap = plan_payload["requirements"][0]["gap"]

    before = await capability.invoke(CapabilityRequest(
        capability_id="research", task_id="task-plan", call_id="assess-1",
        arguments={"operation": "assess"},
    ), context=context)
    assert json.loads(before.output)["ready"] is False

    source = await capability.invoke(CapabilityRequest(
        capability_id="research", task_id="task-plan", call_id="source-1",
        arguments={
            "operation": "record_source", "uri": "https://example.test/release",
            "content": "status=ready", "title": "release snapshot",
        },
    ), context=context)
    source_id = json.loads(source.output)["source"]["id"]
    evidence = await capability.invoke(CapabilityRequest(
        capability_id="research", task_id="task-plan", call_id="evidence-1",
        arguments={
            "operation": "record_evidence", "source_id": source_id,
            "claim_id": "claim-release-status", "claim": "The release is ready.",
            "excerpt": "status=ready",
        },
    ), context=context)
    evidence_id = json.loads(evidence.output)["evidence"]["id"]

    assessed = await capability.invoke(CapabilityRequest(
        capability_id="research", task_id="task-plan", call_id="assess-2",
        arguments={"operation": "assess", "gap_ids": [gap["id"]]},
    ), context=context)
    assessed_payload = json.loads(assessed.output)
    assert assessed_payload["ready"] is True
    assert assessed_payload["gaps"][0]["status"] == "CLOSED"
    assert assessed_payload["gaps"][0]["evidence_ids"] == [evidence_id]

    bundle = await capability.invoke(CapabilityRequest(
        capability_id="research", task_id="task-plan", call_id="bundle-1",
        arguments={"operation": "bundle"},
    ), context=context)
    bundle_payload = json.loads(bundle.output)
    assert bundle_payload["ready"] is True
    assert bundle_payload["evidence"][0]["id"] == evidence_id


@pytest.mark.asyncio
async def test_run_composes_objective_capture_search_evidence_and_verification():
    store = _MemoryResearchStore()
    artifacts = _MemoryArtifacts()
    capability = ResearchCapability(
        store,
        artifact_store=artifacts,
        source_policy=SourcePolicy(allowed_domains=("example.test",)),
    )
    context = SimpleNamespace(workspace=SimpleNamespace(id="repo"))

    result = await capability.invoke(CapabilityRequest(
        capability_id="research", task_id="task-run", call_id="run-1",
        arguments={
            "operation": "run",
            "objective": "verify the release status",
            "requirements": [{
                "id": "release-status",
                "claim_id": "claim-release-status",
                "question": "Is the release ready?",
                "queries": ["status=ready"],
            }],
            "source_specs": [{
                "uri": "https://example.test/release",
                "title": "release snapshot",
                "content": "status=ready",
            }],
            "extractions": [{
                "uri": "https://example.test/release",
                "claim_id": "claim-release-status",
                "claim": "The release is ready.",
                "excerpt": "status=ready",
                "confidence": 0.99,
            }],
        },
    ), context=context)

    assert result.status is CapabilityResultStatus.OK
    payload = json.loads(result.output)
    assert payload["workflow"] == "bounded-research"
    assert payload["capture_errors"] == []
    assert payload["evidence_errors"] == []
    assert payload["search"][0]["content_hits"][0]["source"]["canonical_uri"] == (
        "https://example.test/release"
    )
    assert payload["assessment"]["ready"] is True
    assert payload["bundle"]["ready"] is True
    assert payload["ready"] is True


@pytest.mark.asyncio
async def test_run_keeps_unverified_or_contradictory_research_unready():
    store = _MemoryResearchStore()

    class _MultiArtifacts(_MemoryArtifacts):
        async def save(self, **kwargs):
            content = kwargs["content"]
            if isinstance(content, str):
                content = content.encode()
            digest = hashlib.sha256(content).hexdigest()
            uri = f"artifact://sha256/{digest}"
            self.data[uri] = content
            return ArtifactRef(id=uri, uri=uri, hash=digest)

    artifacts = _MultiArtifacts()
    capability = ResearchCapability(
        store,
        artifact_store=artifacts,
        source_policy=SourcePolicy(allowed_domains=("example.test",)),
    )
    context = SimpleNamespace(workspace=SimpleNamespace(id="repo"))

    first_source_id = SourceRecord.for_uri(
        "https://example.test/one",
        content_hash=hashlib.sha256(b"status=ready").hexdigest(),
    ).id
    first_evidence_id = EvidenceObject.for_content(
        source_id=first_source_id,
        extracted_claim="The release is ready.",
        exact_supporting_excerpt="status=ready",
        claim_id="claim-release-status",
        task_id="task-run-conflict",
    ).id

    result = await capability.invoke(CapabilityRequest(
        capability_id="research", task_id="task-run-conflict", call_id="run-2",
        arguments={
            "operation": "run",
            "objective": "compare release claims",
            "requirements": [{
                "id": "release-status",
                "claim_id": "claim-release-status",
                "question": "What is the release status?",
                "queries": ["status"],
            }],
            "source_specs": [
                {"uri": "https://example.test/one", "content": "status=ready"},
                {"uri": "https://example.test/two", "content": "status=blocked"},
            ],
            "extractions": [
                {
                    "uri": "https://example.test/one",
                    "claim_id": "claim-release-status",
                    "claim": "The release is ready.",
                    "excerpt": "status=ready",
                },
                {
                    "uri": "https://example.test/two",
                    "claim_id": "claim-release-status",
                    "claim": "The release is blocked.",
                    "excerpt": "status=blocked",
                    "contradicts": [first_evidence_id],
                },
            ],
        },
    ), context=context)

    assert result.status is CapabilityResultStatus.OK
    payload = json.loads(result.output)
    assert payload["ready"] is False
    assert payload["assessment"]["ready"] is False
    assert payload["evidence_errors"] == []
    assert payload["assessment"]["gaps"][0]["assessment"]["conflicts"]
    assert payload["bundle"]["ready"] is False


def test_research_effect_contract_is_exact():
    descriptor = ResearchCapability.descriptor
    assert descriptor.resolve_effects({"operation": "sources"})
    assert descriptor.resolve_effects({"operation": "record_source"})
    assert descriptor.resolve_effects({"operation": "fetch"}) == frozenset({
        EffectClass.WRITE_LOCAL,
        EffectClass.NETWORK_READ,
    })
    assert descriptor.resolve_effects({"operation": "plan"}) == frozenset({
        EffectClass.READ_LOCAL,
        EffectClass.WRITE_LOCAL,
    })
    assert descriptor.resolve_effects({"operation": "assess"}) == frozenset({
        EffectClass.READ_LOCAL,
        EffectClass.WRITE_LOCAL,
    })
    assert descriptor.resolve_effects({"operation": "bundle"}) == frozenset({
        EffectClass.READ_LOCAL,
    })
    assert descriptor.resolve_effects({"operation": "run"}) == frozenset({
        EffectClass.READ_LOCAL,
        EffectClass.WRITE_LOCAL,
        EffectClass.NETWORK_READ,
    })
