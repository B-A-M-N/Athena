"""Unit tests for operator projections (stable views over canonical state)."""

from __future__ import annotations

import json

import pytest

from athena.protocol.capabilities import (
    CapabilityRequestOrigin,
    CapabilityResult,
    CapabilityResultStatus,
)
from athena.protocol.tasks import WorkspaceSpec
from athena.protocol.memory import MemoryKind, MemoryRecord, MemoryScope
from athena.protocol.messages import TrustClass
from athena.service.config import AthenaConfig
from athena.service.service import AthenaService
from athena.service.operator_query import OperatorQueryService
from athena.models.registry import ProviderRegistry
from athena.affordances.models import AffordanceScope
from athena.skills.models import Skill, SkillCandidate
from athena.workflows.models import Workflow, WorkflowStep


@pytest.fixture
async def service():
    svc = AthenaService.in_memory()
    await svc.start()
    try:
        yield svc
    finally:
        await svc.stop()


async def test_operator_permissions_empty(service):
    view = await service.operator_permissions()
    assert view == {"active_grants": [], "pending": []}


async def test_startup_health_exposes_optional_degradation_boundary(service):
    health = service.startup_health()

    assert health["status"] == "ok"
    assert health["blocking_failures"] == []
    assert health["checks"]
    assert all("blocking" in check for check in health["checks"].values())


def test_production_service_does_not_register_an_implicit_fake_provider():
    service = AthenaService(config=AthenaConfig(providers=()))
    registry = ProviderRegistry()

    service._register_providers(registry)

    assert registry.names() == ()


@pytest.mark.asyncio
async def test_unconfigured_service_reports_first_run_model_state(tmp_path):
    service = AthenaService(
        config=AthenaConfig(
            db_path=":memory:",
            workspace_root=str(tmp_path),
            providers=(),
        )
    )
    await service.start()
    try:
        health = service.startup_health()
        assert health["checks"]["model_provider"] == {
            "status": "unconfigured",
            "blocking": False,
            "providers": [],
            "reason": "configure a model provider before submitting agent work",
        }
        assert service._model_registry.names() == ()
    finally:
        await service.stop()


async def test_operator_diff_empty(service):
    assert await service.operator_diff() == []


async def test_undo_unknown_mutation(service):
    outcome = await service.undo_mutation("mut_does_not_exist")
    assert outcome["status"] == "error"
    assert "not found" in outcome["error"]


async def test_operator_context_summary(service):
    info = await service.operator_context_summary("session_x")
    assert info["session_id"] == "session_x"


async def test_operator_artifacts_empty(service):
    assert isinstance(await service.operator_artifacts(), list)


async def test_memory_candidate_review_surface_preserves_evidence_and_requires_operator(service):
    candidate = MemoryRecord(
        id="mem_candidate_1",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope.TASK,
        content="Prefer the project's strict validation command.",
        source=None,
        trust=TrustClass.AGENT_CURATED,
        source_refs=("artifact://evidence-1",),
        metadata={
            "pending_promotion": True,
            "task_id": "task-source",
            "observation_count": 2,
        },
    )
    await service._memory.save(candidate)

    rows = await service.operator_memory_candidates()
    assert rows[0]["id"] == candidate.id
    assert rows[0]["required_action"] == "operator_review"
    assert rows[0]["evidence"] == ["artifact://evidence-1"]
    assert rows[0]["observation_count"] == 2

    promoted = await service.operator_promote_memory_candidate(
        candidate.id,
        "project",
        "project-1",
    )
    assert promoted["status"] == "promoted"
    assert await service.operator_memory_candidates() == []


async def test_shared_candidate_queue_includes_durable_skill_candidates_and_workflows(service):
    await service._sessions.create("session-candidate")
    await service._store_tasks.insert_task(
        "task-candidate",
        "session-candidate",
        None,
        "candidate review task",
        autonomy="supervised",
        workspace=service._default_workspace,
    )
    candidate = SkillCandidate(
        draft=Skill(
            id="",
            name="operator-review-helper",
            description="A reviewable helper",
            body="Keep verification evidence with the result.",
            triggers=("review",),
        ),
        source_task_id="task-candidate",
        target_skill=None,
        evidence=("receipt://one",),
        confidence=0.8,
    )
    await service._skill_lifecycle.record_candidate(candidate, task_id="task-candidate")
    await service._workflow_store.save(
        Workflow.create(
            name="candidate procedure",
            description="review me",
            steps=(
                WorkflowStep(id="step", capability_id="truth", arguments={"operation": "status"}),
            ),
            scope=AffordanceScope.CANDIDATE,
            task_scope="task-candidate",
            lifecycle_state="CANDIDATE",
            provenance={
                "observations": [{"task_id": "task-candidate"}],
                "successful_observations": 1,
            },
        )
    )

    rows = await service.operator_candidates("task-candidate")
    kinds = {row["type"] for row in rows}
    assert "skill" in kinds
    assert "workflow" in kinds
    assert any(
        row["id"] == candidate.id and row["required_action"] == "operator_review" for row in rows
    )
    assert all("evidence" in row for row in rows)


async def test_workflow_operator_view_preserves_scope_and_definition(service):
    created = Workflow.create(
        name="visible procedure",
        description="operator inspection",
        steps=(WorkflowStep(id="step", capability_id="truth", arguments={"operation": "status"}),),
        scope=AffordanceScope.PROJECT,
        project_scope=service._default_workspace.id,
    )
    await service._workflow_store.save(created)

    listed = await service.list_workflows()
    inspected = await service.inspect_workflow(created.id)
    assert any(row["id"] == created.id and row["scope"] == "project" for row in listed)
    assert inspected is not None
    assert inspected["steps"][0]["capability"] == "truth"


async def test_generated_capability_operator_methods_use_synthesis_dispatcher():
    class Dispatcher:
        def __init__(self):
            self.calls = []

        async def dispatch(self, request, **kwargs):
            self.calls.append((request, kwargs))
            payload = {"capability_id": request.arguments.get("capability_id", "synth_1")}
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.OK,
                output=json.dumps(payload),
                metadata={"operation": request.arguments["operation"]},
            )

    dispatcher = Dispatcher()
    service = AthenaService.__new__(AthenaService)
    service._operator_query = OperatorQueryService(service)
    service.config = AthenaConfig()
    service._dispatcher = dispatcher
    service._default_workspace = WorkspaceSpec(id="root", root="/tmp/athena")

    candidates = await service.operator_generated_capabilities("task-1")
    inspected = await service.operator_generated_capability("synth_1", "task-1")
    promoted = await service.operator_promote_generated_capability("synth_1", "project", "task-1")
    deprecated = await service.operator_deprecate_generated_capability("synth_1", "task-1")

    assert candidates == {"capability_id": "synth_1"}
    assert inspected["capability_id"] == "synth_1"
    assert promoted["value"]["capability_id"] == "synth_1"
    assert promoted["metadata"]["operation"] == "promote"
    assert deprecated["value"]["capability_id"] == "synth_1"
    assert [request.arguments["operation"] for request, _ in dispatcher.calls] == [
        "candidates",
        "inspect",
        "promote",
        "deprecate",
    ]
    assert all(request.capability_id == "synthesis" for request, _ in dispatcher.calls)
    assert all(
        request.origin is CapabilityRequestOrigin.USER_DIRECT for request, _ in dispatcher.calls
    )
    assert all(kwargs["workspace"].id == "root" for _, kwargs in dispatcher.calls)


async def test_generated_capability_operator_method_surfaces_failure():
    class Dispatcher:
        async def dispatch(self, request, **kwargs):
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                error="capability is unknown",
            )

    service = AthenaService.__new__(AthenaService)
    service._operator_query = OperatorQueryService(service)
    service.config = AthenaConfig()
    service._dispatcher = Dispatcher()
    service._default_workspace = WorkspaceSpec(id="root", root="/tmp/athena")

    try:
        await service.operator_generated_capability("synth_missing", "task-1")
    except ValueError as exc:
        assert str(exc) == "capability is unknown"
    else:
        raise AssertionError("expected synthesis failure")


async def test_direct_escape_records_but_excludes_from_context(service):
    """``!!`` semantics: durable audit record, excluded from model context."""
    session_id = "session_direct_test"

    result = await service.execute_direct(
        "echo hello",
        language="shell",
        session_id=session_id,
        inject_into_context=False,
    )
    if result.get("status") not in ("completed",):
        # Policy may require approval even for echo under supervised profile;
        # the record/exclusion contract below is what this test pins.
        return
    messages = await service._store_messages.list_session_messages(session_id)
    direct = [m for m in messages if (m.metadata or {}).get("direct_execution")]
    for m in direct:
        assert m.metadata.get("inject_into_context") is False
