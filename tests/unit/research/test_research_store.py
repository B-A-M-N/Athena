from __future__ import annotations

import pytest

from athena.research.models import (
    EvidenceObject,
    ResearchGap,
    SourceRecord,
    classify_evidence_quality,
)
from athena.research.store import ResearchStore
from athena.state.database import Database


@pytest.mark.asyncio
async def test_sources_evidence_and_gaps_survive_store_roundtrip(tmp_path):
    db = Database(str(tmp_path / "research.db"))
    store = ResearchStore(db)
    source = SourceRecord.for_uri(
        "artifact://sha256/abc",
        title="captured",
        content_hash="abc",
        artifact_uri="artifact://sha256/abc",
        task_id="task-1",
        revision="etag-1",
        acquisition_method="fixture_capture",
        acquisition_receipt={"status": 200, "content_length": 8},
        confidence_calibration={"method": "two-source-check", "calibrated": True},
    )
    await store.save_source(source)
    evidence = EvidenceObject.for_content(
        source_id=source.id,
        extracted_claim="The captured value is 42.",
        exact_supporting_excerpt="value=42",
        claim_id="claim-1",
        task_id="task-1",
        source_revision="etag-1",
        source_content_hash="abc",
        acquired_at="2026-09-09T00:00:00+00:00",
        confidence_calibration={"method": "two-source-check"},
        contradiction_status="corroborated",
    )
    await store.save_evidence(evidence)
    gap = ResearchGap.create("check the value", "Is the captured value 42?", task_id="task-1")
    await store.save_gap(gap)

    reopened = ResearchStore(db)
    stored_source = await reopened.get_source(source.id)
    assert stored_source.content_hash == "abc"
    assert stored_source.revision == "etag-1"
    assert stored_source.acquisition_method == "fixture_capture"
    assert stored_source.acquisition_receipt["status"] == 200
    assert stored_source.confidence_calibration["calibrated"] is True
    records = await reopened.list_evidence(task_id="task-1", claim_id="claim-1")
    assert records[0].exact_supporting_excerpt == "value=42"
    assert records[0].source_revision == "etag-1"
    assert records[0].source_content_hash == "abc"
    assert records[0].contradiction_status == "corroborated"
    assert (await reopened.list_gaps(task_id="task-1"))[0].id == gap.id
    await db.close()


@pytest.mark.asyncio
async def test_indexed_source_content_is_searchable_and_scoped(tmp_path):
    db = Database(str(tmp_path / "research-index.db"))
    store = ResearchStore(db)
    own = SourceRecord.for_uri(
        "artifact://sha256/own",
        content_hash="own",
        task_id="task-1",
    )
    project = SourceRecord.for_uri(
        "artifact://sha256/project",
        content_hash="project",
        project_id="repo",
    )
    foreign = SourceRecord.for_uri(
        "artifact://sha256/foreign",
        content_hash="foreign",
        task_id="task-2",
    )
    for source, content in (
        (own, "alpha repair boundary"),
        (project, "alpha project procedure"),
        (foreign, "alpha private procedure"),
    ):
        await store.save_source(source)
        await store.index_content(
            source.id, content, content_hash=source.content_hash or "", mime_type="text/plain"
        )

    hits = await store.search_content(
        "repair",
        task_id="task-1",
        project_id="repo",
        limit=10,
    )
    assert [hit["source"]["id"] for hit in hits] == [own.id]
    assert "repair boundary" in hits[0]["snippet"]

    project_hits = await store.search_content(
        "procedure",
        task_id="task-1",
        project_id="repo",
        limit=10,
    )
    assert {hit["source"]["id"] for hit in project_hits} == {project.id}
    await db.close()


def test_evidence_quality_classifies_support_staleness_and_conflict():
    source = SourceRecord.for_uri(
        "artifact://sha256/current",
        content_hash="current",
        artifact_uri="artifact://sha256/current",
        revision="rev-2",
    )
    supported = EvidenceObject.for_content(
        source_id=source.id,
        extracted_claim="claim",
        exact_supporting_excerpt="excerpt",
        source_revision="rev-2",
        source_content_hash="current",
        confidence=0.9,
    )
    assert classify_evidence_quality(supported, source) == "supported"
    assert (
        classify_evidence_quality(
            EvidenceObject.for_content(
                source_id=source.id,
                extracted_claim="claim",
                exact_supporting_excerpt="excerpt",
                source_revision="rev-1",
                source_content_hash="current",
                confidence=0.9,
            ),
            source,
        )
        == "stale"
    )
    assert (
        classify_evidence_quality(
            EvidenceObject.for_content(
                source_id=source.id,
                extracted_claim="claim",
                exact_supporting_excerpt="excerpt",
                source_revision="rev-2",
                source_content_hash="current",
                confidence=0.2,
            ),
            source,
        )
        == "weak"
    )
    assert (
        classify_evidence_quality(
            EvidenceObject.for_content(
                source_id=source.id,
                extracted_claim="claim",
                exact_supporting_excerpt="excerpt",
                source_revision="rev-2",
                source_content_hash="current",
                contradiction_status="contradicted",
            ),
            source,
        )
        == "contradicted"
    )
    assert classify_evidence_quality(supported, None) == "no_evidence"
