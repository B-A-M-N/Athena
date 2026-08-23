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
    svc = await make_service(scripts=[
        # After any ok capability result: stop the (parent) loop.
        {"match": {"capability_result_ok": True},
         "respond": {"text": "PARENT_DONE", "done": True}},
        # The child's own objective: complete immediately.
        {"match": {"user_contains": "CHILD_MATH"},
         "respond": {"text": "CHILD_DONE", "done": True}},
        # The parent spawns a child on its first turn.
        {"match": {"user_contains": "DELEGATE_PARENT"},
         "respond": {"capability_call": {
             "capability_id": "delegate",
             "arguments": {"operation": "spawn",
                           "objective": "CHILD_MATH compute a result"},
         }}},
    ])

    parent = await svc.submit(
        AgentRequest(prompt="DELEGATE_PARENT go", session_id=new_id("session"),
                     autonomy=AutonomyLevel.AUTONOMOUS),
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
    rows = await svc._db.fetch_all(
        "SELECT id FROM tasks WHERE parent_task_id = ?", (parent.id,))
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
            TaskStatus.COMPLETE.value, TaskStatus.PARTIAL.value,
            TaskStatus.FAILED.value, TaskStatus.CANCELLED.value,
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