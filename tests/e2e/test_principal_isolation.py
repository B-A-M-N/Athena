"""Durable principal ownership journey.

This is deliberately a service-level proof: state is written through the
durable subsystem stores, the service is stopped, a different principal is
started against the same database, and the original principal is restarted.
It catches the split-brain failure mode where each subsystem is individually
wired but uses a different owner identity.
"""

from __future__ import annotations

import pytest

from athena.affordances.models import AffordanceScope, GeneratedCapability
from athena.protocol.memory import MemoryKind, MemoryRecord, MemoryScope
from athena.protocol.messages import Provenance, SourceType, TrustClass
from athena.protocol.ids import new_id
from athena.workflows.models import Workflow, WorkflowStep


async def _seed_alice_state(service) -> dict[str, str]:
    owner = service.config.cache_namespace
    project_id = service._default_workspace.id

    await service._sessions.create(
        f"{owner}-session",
        principal_id=owner,
        project_id=project_id,
        metadata={"journey": "principal-isolation"},
    )
    await service._memory.save(
        MemoryRecord(
            id=new_id("mem"),
            kind=MemoryKind.SEMANTIC,
            scope=MemoryScope.USER,
            content="ALICE_DURABLE_USER_MEMORY",
            source=Provenance(
                source_type=SourceType.USER,
                source_id=owner,
                trust=TrustClass.USER_CONTENT,
                scope=owner,
            ),
            trust=TrustClass.USER_CONTENT,
            metadata={"scope_id": owner, "journey": "principal-isolation"},
        )
    )
    await service._context_block_store.create(
        label="alice durable context",
        content="ALICE_DURABLE_CONTEXT_BLOCK",
        scope=MemoryScope.USER.value,
        scope_id=owner,
        trust=TrustClass.USER_CONTENT,
        metadata={"journey": "principal-isolation"},
    )

    workflow = Workflow.create(
        name="alice durable workflow",
        description="A user-owned workflow retained across service restarts.",
        steps=(
            WorkflowStep(
                id="inspect",
                capability_id="fs",
                arguments={"operation": "list", "path": "."},
            ),
        ),
        scope=AffordanceScope.USER,
        user_scope=owner,
        provenance={"journey": "principal-isolation"},
    )
    await service._workflow_store.save(workflow)

    generated = GeneratedCapability(
        id="alice-durable-generated",
        name="alice_durable_generated",
        description="A proven user-owned generated capability.",
        implementation='def run(args):\n    return {"owner": "alice"}\n',
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        declared_effects=frozenset({"READ_LOCAL"}),
        effective_authority=frozenset({"READ_LOCAL"}),
        scope=AffordanceScope.USER,
        user_scope=owner,
        provenance={"journey": "principal-isolation"},
        validation_state="PROMOTED",
        lifecycle_state="ACTIVE",
        proof_record={"all_passed": True, "journey": "principal-isolation"},
    )
    await service._generated_store.save(generated, owner=owner)
    return {"workflow_id": workflow.id, "generated_id": generated.id}


async def _snapshot(service, owner: str, generated_id: str) -> dict[str, object]:
    memories = await service._memory.list_by_scope(MemoryScope.USER, owner)
    blocks = await service._context_block_store.list(
        scopes=[(MemoryScope.USER.value, owner)],
    )
    workflows = await service._workflow_store.list(user_id=owner)
    generated = await service._generated_store.list(user_id=owner)
    return {
        "memory": [record.content for record in memories],
        "context": [block.content for block in blocks],
        "workflows": [workflow.name for workflow in workflows],
        "generated": [capability.id for capability in generated],
        "rehydrated": service._fabric.has(generated_id, user_id=owner),
    }


@pytest.mark.athena_claim("P1-11")
@pytest.mark.athena_evidence("test", "e2e")
@pytest.mark.athena_scenario("PRINCIPAL-003")
@pytest.mark.asyncio
async def test_user_scoped_state_isolated_across_service_restarts(
    durable_db_path,
    make_durable_service,
):
    """Alice's durable state survives; Bob cannot see or rehydrate it."""
    alice = await make_durable_service(durable_db_path, cache_namespace="alice")
    ids = await _seed_alice_state(alice)
    workspace = alice._default_workspace.root
    alice_snapshot = await _snapshot(alice, "alice", ids["generated_id"])
    assert "ALICE_DURABLE_USER_MEMORY" in alice_snapshot["memory"]
    assert "ALICE_DURABLE_CONTEXT_BLOCK" in alice_snapshot["context"]
    assert "alice durable workflow" in alice_snapshot["workflows"]
    assert ids["generated_id"] in alice_snapshot["generated"]
    assert alice_snapshot["rehydrated"] is False  # saved after startup
    assert alice._compiler.principal_id == "alice"
    await alice.stop()

    bob = await make_durable_service(
        durable_db_path,
        cache_namespace="bob",
        workspace_root=workspace,
    )
    bob_snapshot = await _snapshot(bob, "bob", ids["generated_id"])
    assert bob_snapshot == {
        "memory": [],
        "context": [],
        "workflows": [],
        "generated": [],
        "rehydrated": False,
    }
    assert await bob._generated_store.get(ids["generated_id"], user_id="bob") is None
    assert bob._compiler.principal_id == "bob"
    await bob.stop()

    alice_again = await make_durable_service(
        durable_db_path,
        cache_namespace="alice",
        workspace_root=workspace,
    )
    final_snapshot = await _snapshot(alice_again, "alice", ids["generated_id"])
    assert "ALICE_DURABLE_USER_MEMORY" in final_snapshot["memory"]
    assert "ALICE_DURABLE_CONTEXT_BLOCK" in final_snapshot["context"]
    assert "alice durable workflow" in final_snapshot["workflows"]
    assert ids["generated_id"] in final_snapshot["generated"]
    assert final_snapshot["rehydrated"] is True
    assert alice_again._compiler.principal_id == "alice"
