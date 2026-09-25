"""Delegation cross-subsystem flow: parent -> child, scoped and isolated.

The ``delegate.spawn`` capability resolves to the SPAWN_PROCESS effect which has
no explicit allow rule in the built-in profiles (default ASK), so the parent
parks for approval the same way any effectful call does. After the grant the
child is created with a FRESH session and runs through the SAME worker/kernel.
"""

from __future__ import annotations
import pytest

from athena.protocol.tasks import AgentRequest, AutonomyLevel, TaskStatus
from athena.protocol.ids import new_id


@pytest.mark.athena_claim("BHV-087")
@pytest.mark.athena_evidence("test", "e2e")
async def test_parent_delegates_child_with_isolated_session(make_service):
    svc = await make_service(
        scripts=[
            # After any ok capability result: stop the (parent) loop.
            {
                "match": {"capability_result_ok": True},
                "respond": {"text": "PARENT_DONE", "done": True},
            },
            # The child's objective is conversational, so it can complete
            # without a capability result.
            {
                "match": {"user_contains": "CHILD_MATH"},
                "respond": {"text": "CHILD_DONE", "done": True},
            },
            # The parent spawns a child on its first turn.
            {
                "match": {"user_contains": "DELEGATE_PARENT"},
                "respond": {
                    "capability_call": {
                        "capability_id": "delegate",
                        "arguments": {
                            "operation": "spawn",
                            "objective": "CHILD_MATH",
                        },
                    }
                },
            },
        ]
    )

    parent = await svc.submit(
        AgentRequest(
            prompt="DELEGATE_PARENT go",
            session_id=new_id("session"),
            autonomy=AutonomyLevel.AUTONOMOUS,
        ),
        wait=False,
    )

    # Parent parks on the delegate ask; approve to let it spawn.
    for _ in range(150):
        if (await svc.get_task_status(parent.id)) == TaskStatus.WAITING_APPROVAL.value:
            break
        from asyncio import sleep

        await sleep(0.02)
    approval_id = await svc.pending_approval_id(parent.id)
    assert approval_id is not None
    await svc.approve(approval_id, granted=True)

    pfinal = await svc.wait_for(parent.id)
    assert (pfinal.metadata or {}).get("status") == TaskStatus.COMPLETE.value

    # A child Task exists, pointing at the parent.
    rows = await svc._db.fetch_all("SELECT id FROM tasks WHERE parent_task_id = ?", (parent.id,))
    assert len(rows) == 1
    child_id = rows[0]["id"]

    child = await svc.get_task(child_id)
    assert child.parent_task_id == parent.id
    assert child.session_id is not None
    # Lineage-only session: the child carries its OWN session, not the parent's.
    assert child.session_id != parent.session_id

    # Worker + kernel drive the child to completion.
    for _ in range(200):
        if (await svc.get_task_status(child_id)) in (
            TaskStatus.COMPLETE.value,
            TaskStatus.PARTIAL.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
        ):
            break
        from asyncio import sleep

        await sleep(0.03)
    assert await svc.get_task_status(child_id) == TaskStatus.COMPLETE.value

    # Isolation: the child's capability policy is a subset of the parent's
    # (children never inherit MORE authority than their parent).
    cp, pp = child.capability_policy, pfinal.capability_policy
    assert set(cp.allow).issubset(set(pp.allow))
    assert set(cp.ask).issubset(set(pp.ask))
    assert set(cp.effects).issubset(set(pp.effects))


@pytest.mark.athena_claim("BHV-087")
@pytest.mark.athena_evidence("test", "e2e")
async def test_delegation_collect_returns_child_conclusion_to_parent(make_service):
    """delegate.collect must hand the child's real conclusion to the parent.

    A fresh-context child that solved its objective carries its semantic
    answer in TaskResult.summary; the parent's tool result formats exactly
    that summary. Without this, a successful delegated investigation returns
    "child task_x COMPLETE: objective satisfied" — the pattern is crippled.
    """
    svc = await make_service(
        scripts=[
            # After any ok capability result: stop the loop with the
            # observation on record (spawn's ok result stops the parent).
            {
                "match": {"capability_result_ok": True},
                "respond": {"text": "PARENT_DONE", "done": True},
            },
            # The child completes with a distinctive conclusion.
            {
                "match": {"user_contains": "CHILD_INVESTIGATE"},
                "respond": {
                    "text": "ROOT_CAUSE: cache invalidation path skips mtime check",
                    "done": True,
                },
            },
            # Parent spawns the child, then collects it in the same run.
            {
                "match": {"user_contains": "DELEGATE_AND_COLLECT"},
                "respond": {
                    "capability_call": {
                        "capability_id": "delegate",
                        "arguments": {
                            "operation": "spawn",
                            "objective": "CHILD_INVESTIGATE the failing module",
                        },
                    }
                },
            },
            {
                "match": {"capability_id": "delegate", "operation": "collect"},
            },
        ]
    )

    # Drive collect directly through the delegation handle to observe the
    # formatted handback, because the scripted parent model in this suite
    # stops after spawn. The capability-level contract under test is the
    # same _format_result path a model-visible collect call renders.
    parent = await svc.submit(
        AgentRequest(
            prompt="DELEGATE_AND_COLLECT now",
            session_id=new_id("session"),
            autonomy=AutonomyLevel.AUTONOMOUS,
        ),
        wait=False,
    )
    for _ in range(150):
        if (await svc.get_task_status(parent.id)) == TaskStatus.WAITING_APPROVAL.value:
            break
        from asyncio import sleep

        await sleep(0.02)
    approval_id = await svc.pending_approval_id(parent.id)
    assert approval_id is not None
    await svc.approve(approval_id, granted=True)
    await svc.wait_for(parent.id)

    rows = await svc._db.fetch_all("SELECT id FROM tasks WHERE parent_task_id = ?", (parent.id,))
    assert len(rows) == 1
    child_id = rows[0]["id"]
    for _ in range(200):
        if (await svc.get_task_status(child_id)) == TaskStatus.COMPLETE.value:
            break
        from asyncio import sleep

        await sleep(0.03)
    assert await svc.get_task_status(child_id) == TaskStatus.COMPLETE.value

    # The child's durable result preserves its real final text (not the
    # "objective satisfied" reason fallback).
    child_result = await svc.get_result(child_id)
    assert child_result is not None
    assert "ROOT_CAUSE" in child_result.summary
    assert "objective satisfied" not in child_result.summary

    # delegate.collect formats that summary into the parent-visible output.
    # The parent-scoped delegation handle is the same one the capability
    # resolves for a model-issued collect call, so invoking the capability
    # directly exercises the identical formatting contract.
    from athena.capabilities.delegate import DelegateCapability

    collect = await DelegateCapability(svc._delegation).invoke(
        _collect_request(parent.id, child_id),
        context=None,
    )

    from athena.protocol.capabilities import CapabilityResultStatus

    assert collect.status is CapabilityResultStatus.OK, collect.error
    assert "ROOT_CAUSE: cache invalidation path skips mtime check" in (collect.output or "")
    assert "objective satisfied" not in (collect.output or "")


def _collect_request(parent_task_id: str, child_task_id: str):
    from athena.protocol.capabilities import CapabilityRequest
    from athena.protocol.ids import new_id

    return CapabilityRequest(
        capability_id="delegate",
        task_id=parent_task_id,
        call_id=new_id("call"),
        arguments={"operation": "collect", "child_task_id": child_task_id},
    )


@pytest.mark.asyncio
async def test_delegation_matches_single_agent_outcome_with_child_evidence(make_service):
    """Compare identical work with and without delegation on the real service."""
    from athena.evaluation.neutral import (
        EvaluationCase,
        EvaluationOutcome,
        compare_delegation_outcomes,
    )
    from athena.protocol.tasks import AgentRequest, AutonomyLevel, TaskStatus, TaskSpec
    from athena.protocol.ids import new_id

    objective = "DELEGATION_COMPARISON calculate the fixed value"
    direct = await make_service(
        scripts=[
            {
                "match": {"last_user_message_contains": "DELEGATION_COMPARISON"},
                "respond": {"text": "DIRECT_RESULT: 41", "done": True},
            }
        ]
    )
    direct_task = await direct.submit(
        AgentRequest(prompt=objective, autonomy=AutonomyLevel.AUTONOMOUS), wait=True
    )
    direct_result = await direct.get_result(direct_task.id)
    assert direct_result is not None
    assert direct_result.status is TaskStatus.COMPLETE

    delegated = await make_service(
        scripts=[
            {
                "match": {"user_contains": "calculate the fixed value"},
                "respond": {"text": "CHILD_RESULT: 41", "done": True},
            },
        ]
    )
    parent = await delegated._task_manager.create(
        TaskSpec(
            id=new_id("task"),
            objective=objective,
            session_id=new_id("session"),
            metadata={"autonomy": AutonomyLevel.AUTONOMOUS.value},
        )
    )
    child_id = await delegated._delegation.spawn_child(
        objective="calculate the fixed value",
        parent_task_id=parent.id,
    )
    for _ in range(200):
        if await delegated.get_task_status(child_id) == TaskStatus.COMPLETE.value:
            break
        from asyncio import sleep

        await sleep(0.02)
    child_result = await delegated.get_result(child_id)
    assert child_result is not None
    assert child_result.status is TaskStatus.COMPLETE
    assert "CHILD_RESULT: 41" in child_result.summary

    from athena.capabilities.delegate import DelegateCapability
    from athena.protocol.capabilities import CapabilityRequest, CapabilityResultStatus

    collected = await DelegateCapability(delegated._delegation).invoke(
        CapabilityRequest(
            capability_id="delegate",
            task_id=parent.id,
            call_id=new_id("call"),
            arguments={"operation": "collect", "child_task_id": child_id, "timeout": 1.0},
        )
    )
    assert collected.status is CapabilityResultStatus.OK, collected.error
    assert "CHILD_RESULT: 41" in (collected.output or "")
    await delegated._task_manager.enqueue(parent.id)
    parent_result = await delegated._task_manager.finalize(
        parent.id,
        status=TaskStatus.COMPLETE,
        summary="PARENT_RECEIVED_CHILD",
    )
    assert parent_result.status is TaskStatus.COMPLETE

    case = EvaluationCase(
        id="delegation-comparison",
        prompt=objective,
        required_evidence=("result-41",),
    )
    report = compare_delegation_outcomes(
        case,
        single_agent=EvaluationOutcome(
            case_id=case.id,
            status="complete",
            evidence=("result-41",),
            model_calls=1,
        ),
        delegated=EvaluationOutcome(
            case_id=case.id,
            status="complete",
            evidence=("result-41",),
            model_calls=1,
            delegation_count=1,
            metadata={"child_evidence": ("result-41",), "child_id": child_id},
        ),
    )
    comparison = report["comparison"]
    assert comparison["single_agent_completed"] is True
    assert comparison["delegated_completed"] is True
    assert comparison["child_evidence_verified"] is True
    assert comparison["delegation_count"] == 1
    assert comparison["materially_contributed"] is None
