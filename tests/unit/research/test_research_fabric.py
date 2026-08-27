from __future__ import annotations

import json
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

    async def save_source(self, source):
        self.sources[source.id] = source
        return source

    async def get_source(self, source_id):
        return self.sources.get(source_id)

    async def list_sources(self, **kwargs):
        return list(self.sources.values())

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

    async def save(self, **kwargs):
        content = kwargs["content"]
        if isinstance(content, str):
            content = content.encode()
        uri = "artifact://sha256/source"
        self.data[uri] = content
        return ArtifactRef(id=uri, uri=uri, hash="source")

    async def load(self, ref):
        return self.data[ref]


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
