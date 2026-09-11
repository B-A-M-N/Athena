"""End-to-end wiring tests: knowledge pipeline, criteria, role selection."""

from __future__ import annotations

import asyncio

import pytest

from athena.service.service import AthenaService
from athena.service.task_api import TaskAPI
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResultStatus,
)
from athena.protocol.tasks import TaskStatus
from athena.research.models import EvidenceObject, SourceRecord


@pytest.fixture
async def service():
    svc = AthenaService.in_memory()
    await svc.start()
    try:
        yield svc
    finally:
        await svc.stop()


async def test_finalize_observer_registered(service):
    """The knowledge pipeline is bound to the single finalization authority."""
    observers = service._task_manager._finalize_observers
    assert any(type(o).__name__ == "KnowledgePipeline" for o in observers)


async def test_completed_task_records_episodic_memory(service):
    spec = await service.submit(_req("remember that banana is my favorite word"), wait=True)
    rows = await service._store_tasks.list_by_status(TaskStatus.COMPLETE)
    assert any(r["id"] == spec.id for r in rows)

    # The knowledge pipeline is async post-finalization; poll briefly.
    content: list[str] = []
    for _ in range(40):
        episodic = await service._memory.retrieve_by_recency(None, None, 100)
        content = [r.content for r in episodic]
        if any(spec.id in c or "banana" in c for c in content):
            return
        await asyncio.sleep(0.1)
    pytest.fail(f"episodic memory not recorded; got {content!r}")


async def test_submit_records_one_canonical_user_turn_before_enqueue(service):
    spec = await service.submit(_req("remember this intake"), wait=False)

    messages = await service._store_messages.list_session_messages(spec.session_id)
    user_turns = [
        message
        for message in messages
        if (message.metadata or {}).get("canonical_user_turn") is True
    ]
    assert len(user_turns) == 1
    assert user_turns[0].metadata["task_id"] == spec.id
    assert user_turns[0].text() == "remember this intake"


async def test_created_intake_is_restart_reconcilable_after_canonical_write_crash(
    service, monkeypatch
):
    request = _req("recover this interrupted intake")
    request = type(request)(prompt=request.prompt, session_id="session-intake-crash")
    spec = service._build_task_spec(request, request.session_id)
    original = service._record_canonical_user_turn

    async def crash(*args, **kwargs):
        raise RuntimeError("crash between task row and canonical turn")

    monkeypatch.setattr(service, "_record_canonical_user_turn", crash)
    with pytest.raises(RuntimeError, match="canonical turn"):
        await service._enqueue_spec(
            service._task_manager,
            spec,
            wait=False,
            user_request=request,
        )

    created = await service._store_tasks.get(spec.id)
    assert created is not None
    assert created["status"] == TaskStatus.CREATED.value
    assert created["metadata"]["_intake_phase"] == "task_created"

    monkeypatch.setattr(service, "_record_canonical_user_turn", original)
    result = await service._reconcile_created_intake()
    assert result == {"recovered": 1, "quarantined": 0, "skipped": 0}

    recovered = await service._store_tasks.get(spec.id)
    assert recovered is not None
    assert recovered["status"] != TaskStatus.CREATED.value
    assert recovered["metadata"]["_intake_phase"] == "enqueued"
    messages = await service._store_messages.list_session_messages(spec.session_id)
    canonical = [
        message
        for message in messages
        if (message.metadata or {}).get("canonical_user_turn") is True
    ]
    assert len(canonical) == 1
    assert canonical[0].id == f"msg_user_{spec.id}"


async def test_existing_created_task_retries_the_same_intake_protocol(service, monkeypatch):
    request = _req("retry the same durable intake")
    request = type(request)(prompt=request.prompt, session_id="session-intake-existing")
    spec = service._build_task_spec(request, request.session_id)
    original = service._record_canonical_user_turn

    async def crash(*args, **kwargs):
        raise RuntimeError("canonical turn write interrupted")

    monkeypatch.setattr(service, "_record_canonical_user_turn", crash)
    with pytest.raises(RuntimeError, match="canonical turn"):
        await service._enqueue_spec(
            service._task_manager,
            spec,
            wait=False,
            user_request=request,
        )

    monkeypatch.setattr(service, "_record_canonical_user_turn", original)
    retried = await service._enqueue_spec(
        service._task_manager,
        spec,
        wait=False,
        user_request=request,
    )

    assert retried.id == spec.id
    row = await service._store_tasks.get(spec.id)
    assert row is not None and row["status"] != TaskStatus.CREATED.value
    messages = await service._store_messages.list_session_messages(spec.session_id)
    canonical = [
        message
        for message in messages
        if (message.metadata or {}).get("canonical_user_turn") is True
    ]
    assert len(canonical) == 1
    assert canonical[0].id == f"msg_user_{spec.id}"


async def test_created_intake_recovers_after_enqueue_failure(service, monkeypatch):
    request = _req("recover after enqueue failure")
    request = type(request)(prompt=request.prompt, session_id="session-intake-enqueue")
    spec = service._build_task_spec(request, request.session_id)
    original_enqueue = service._task_manager.enqueue

    async def crash(task_id):
        raise RuntimeError("enqueue interrupted")

    monkeypatch.setattr(service._task_manager, "enqueue", crash)
    with pytest.raises(RuntimeError, match="enqueue interrupted"):
        await service._enqueue_spec(
            service._task_manager,
            spec,
            wait=False,
            user_request=request,
        )

    created = await service._store_tasks.get(spec.id)
    assert created is not None
    assert created["status"] == TaskStatus.CREATED.value
    assert created["metadata"]["_intake_phase"] == "canonical_user_turn_persisted"

    monkeypatch.setattr(service._task_manager, "enqueue", original_enqueue)
    result = await service._reconcile_created_intake()
    assert result == {"recovered": 1, "quarantined": 0, "skipped": 0}
    recovered = await service._store_tasks.get(spec.id)
    assert recovered is not None
    assert recovered["status"] != TaskStatus.CREATED.value
    assert recovered["metadata"]["_intake_phase"] == "enqueued"


async def test_intake_remains_queued_if_phase_receipt_fails_after_enqueue(service, monkeypatch):
    request = _req("recover after enqueue receipt failure")
    request = type(request)(prompt=request.prompt, session_id="session-intake-receipt")
    spec = service._build_task_spec(request, request.session_id)
    original_mark = TaskAPI._mark_intake_phase

    async def crash_after_enqueue(owner, task_id, phase):
        if phase == "enqueued":
            raise RuntimeError("enqueue receipt interrupted")
        await original_mark(owner, task_id, phase)

    monkeypatch.setattr(TaskAPI, "_mark_intake_phase", crash_after_enqueue)
    with pytest.raises(RuntimeError, match="enqueue receipt interrupted"):
        await service._enqueue_spec(
            service._task_manager,
            spec,
            wait=False,
            user_request=request,
        )

    row = await service._store_tasks.get(spec.id)
    assert row is not None
    assert row["status"] != TaskStatus.CREATED.value
    messages = await service._store_messages.list_session_messages(spec.session_id)
    canonical = [
        message
        for message in messages
        if (message.metadata or {}).get("canonical_user_turn") is True
    ]
    assert len(canonical) == 1


def _req(prompt: str):
    from athena.protocol.tasks import AgentRequest

    return AgentRequest(prompt=prompt)


async def test_criteria_metadata_builds_required_criteria(service):
    request = _req("do the thing")
    request = type(request)(
        prompt=request.prompt,
        metadata={"acceptance_criteria": ["command:true", "the file exists"]},
    )
    session_id = "session_criteria"
    spec = service._build_task_spec(request, session_id)
    crits = spec.acceptance_criteria
    assert len(crits) == 2
    assert all(c.required for c in crits)
    assert crits[0].verification.command == "true"
    assert crits[1].verification.predicate == "the file exists"


async def test_role_parsing(service):
    policies = service._role_policies(
        {
            "summarizer": {"allowed": ["prov/cheap"], "max_cost_usd": "0.05"},
            "bad": "not-a-table",
        }
    )
    assert policies["summarizer"].allowed == ("prov/cheap",)
    assert "bad" not in policies


async def test_fusion_is_registered_on_the_model_dispatch_surface(service):
    task = await service.submit(_req("prepare a fusion surface probe"), wait=True)
    assert service._fabric.has("fusion", task_id=task.id)
    result = await service._dispatcher.dispatch(
        CapabilityRequest(
            capability_id="fusion",
            arguments={"operation": "status", "branch_id": "missing-branch"},
            task_id=task.id,
            call_id="fusion-status",
            origin=CapabilityRequestOrigin.MODEL,
        ),
        workspace=service._default_workspace,
    )

    # The request reached FusionCapability through the real dispatcher. A
    # missing branch is an executor-level failure, not an unavailable tool or
    # an effect-envelope rejection.
    assert result.status is CapabilityResultStatus.FAILED
    assert result.error == "branch not found"


async def test_acceptance_evidence_includes_task_scoped_research(service):
    task = await service.submit(_req("verify a captured research claim"), wait=True)
    content = b"status=ready"
    artifact = await service._artifacts.save(
        task_id=task.id,
        content=content,
        mime_type="text/plain",
        producer="test.research",
    )
    source = SourceRecord.for_uri(
        artifact.uri,
        title="captured release",
        content_hash=artifact.hash,
        artifact_uri=artifact.uri,
        task_id=task.id,
    )
    await service._research_store.save_source(source)
    evidence = EvidenceObject.for_content(
        source_id=source.id,
        claim_id="release-status",
        extracted_claim="The release is ready.",
        exact_supporting_excerpt="status=ready",
        task_id=task.id,
    )
    await service._research_store.save_evidence(evidence)

    collected = await service._verification_evidence(task)
    research = collected["evidence"]["research"]
    assert research["sources"][0]["id"] == source.id
    assert research["evidence"][0]["id"] == evidence.id
    assert collected["world_state"]["task_id"] == task.id
    assert collected["evidence"]["world_state"]["task_id"] == task.id
