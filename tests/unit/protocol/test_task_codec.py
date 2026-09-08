from __future__ import annotations

from decimal import Decimal

import pytest

from athena.api.app import build_agent_request
from athena.kernel.lifecycle import deserialize_task
from athena.protocol.task_codec import decode_task_spec, encode_task_spec
from athena.protocol.tasks import (
    AgentRequest,
    Criterion,
    ModelPolicy,
    MutationMode,
    VerificationSpec,
    VerificationType,
    WorkspaceSpec,
)
from athena.service.service import AthenaService
from athena.state.database import Database
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore


def _criterion() -> Criterion:
    return Criterion(
        id="acceptance-1",
        description="the generated artifact exists",
        verification=VerificationSpec(
            type=VerificationType.FILE,
            path="artifact.txt",
        ),
        required=True,
    )


def test_http_boundary_exposes_typed_authority_fields():
    request = build_agent_request(
        {
            "prompt": "typed authority",
            "mutation_mode": "read_only",
            "acceptance_criteria": [
                {
                    "id": "acceptance-1",
                    "description": "the generated artifact exists",
                    "required": True,
                    "verification": {"type": "file", "path": "artifact.txt"},
                }
            ],
            "model_policy": {"min_quality_tier": "premium"},
        }
    )
    assert request.mutation_mode is MutationMode.READ_ONLY
    assert request.acceptance_criteria == (_criterion(),)
    assert request.model_policy is not None
    assert request.model_policy.min_quality_tier == "premium"


@pytest.mark.asyncio
async def test_typed_task_authority_round_trips_through_db_and_recovery(tmp_path):
    service = AthenaService.in_memory()
    workspace = WorkspaceSpec(
        id="typed-workspace",
        root=str(tmp_path),
        mutation_mode=MutationMode.READ_ONLY,
        delegate_mode="DETACHED",
        required_child=False,
    )
    model_policy = ModelPolicy(
        allowed=("local/model",),
        min_quality_tier="premium",
        max_cost_usd=Decimal("1.25"),
    )
    request = AgentRequest(
        prompt="typed authority",
        workspace=workspace,
        mutation_mode=MutationMode.READ_ONLY,
        acceptance_criteria=(_criterion(),),
        model_policy=model_policy,
    )
    spec = service._build_task_spec(request, "typed-session")
    assert spec.workspace == workspace
    assert spec.acceptance_criteria == (_criterion(),)

    db = Database(str(tmp_path / "tasks.sqlite"))
    await db._ensure_ready()
    sessions = SessionRepository(db)
    await sessions.create("typed-session")
    tasks = TaskStore(db)
    await tasks.insert_task(
        spec.id,
        spec.session_id,
        spec.parent_task_id,
        spec.objective,
        acceptance_criteria=spec.acceptance_criteria,
        context_refs=spec.context_refs,
        workspace=spec.workspace,
        capability_policy=spec.capability_policy,
        model_policy=spec.model_policy,
        resource_budget=spec.resource_budget,
        deadline=spec.deadline,
        delivery=spec.delivery,
        metadata=dict(spec.metadata),
    )
    row = await tasks.get(spec.id)
    assert row is not None
    restored = deserialize_task(row)
    assert restored.workspace == workspace
    assert restored.acceptance_criteria == (_criterion(),)
    assert restored.model_policy == model_policy

    canonical = encode_task_spec(restored)
    decoded = decode_task_spec(canonical)
    assert decoded.workspace == workspace
    assert decoded.acceptance_criteria == (_criterion(),)
    assert decoded.model_policy == model_policy
    await db.close()
