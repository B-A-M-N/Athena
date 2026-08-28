from __future__ import annotations

import pytest

from athena.research.models import EvidenceObject, ResearchGap, SourceRecord
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
    )
    await store.save_source(source)
    evidence = EvidenceObject.for_content(
        source_id=source.id,
        extracted_claim="The captured value is 42.",
        exact_supporting_excerpt="value=42",
        claim_id="claim-1",
        task_id="task-1",
    )
    await store.save_evidence(evidence)
    gap = ResearchGap.create("check the value", "Is the captured value 42?", task_id="task-1")
    await store.save_gap(gap)

    reopened = ResearchStore(db)
    assert (await reopened.get_source(source.id)).content_hash == "abc"
    records = await reopened.list_evidence(task_id="task-1", claim_id="claim-1")
    assert records[0].exact_supporting_excerpt == "value=42"
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
