"""Approval flow through the real AthenaService + dispatcher + ApprovalStore.

Under the default SUPERVISED profile an ``execute`` capability call is an ASK
(policy profile), so the kernel parks the task in WAITING_APPROVAL. Resolving
the approval grants an exact scoped grant and resumes the task.
"""
from __future__ import annotations

from athena.protocol.tasks import AgentRequest, TaskStatus
from athena.protocol.ids import new_id


async def _collect_up_to(svc, task_id, target: str, tries=150, delay=0.02) -> str | None:
    for _ in range(tries):
        st = await svc.get_task_status(task_id)
        if st == target:
            return st
        from asyncio import sleep
        await sleep(delay)
    return await svc.get_task_status(task_id)


async def _request_execution(svc, prompt: str):
    return await svc.submit(
        AgentRequest(prompt=prompt, session_id=new_id("session")), wait=False
    )


async def test_approve_granted_resumes_and_completes(make_service):
    svc = await make_service(scripts=[
        {"match": {"capability_result_ok": True},
         "respond": {"text": "EXECUTED_OK", "done": True}},
        {"match": {"user_contains": "APPROVE_EXEC"},
         "respond": {"capability_call": {
             "capability_id": "execute",
             "arguments": {"language": "sh", "code": "echo ok"},
         }}},
    ])
    task = await _request_execution(svc, "APPROVE_EXEC do it")
    assert await _collect_up_to(svc, task.id, TaskStatus.WAITING_APPROVAL.value) \
        == TaskStatus.WAITING_APPROVAL.value

    approval_id = await svc.pending_approval_id(task.id)
    assert approval_id is not None

    await svc.approve(approval_id, granted=True)
    final = await svc.wait_for(task.id)
    assert (final.metadata or {}).get("status") == TaskStatus.COMPLETE.value
    events = [e async for e in svc.stream_events(task.id)]
    assert any(e.type == "CapabilityCompleted" for e in events)


async def test_approve_denied_has_no_effect(make_service):
    svc = await make_service(scripts=[
        {"match": {"capability_result_ok": False},
         "respond": {"text": "AFTER_DENY", "done": True}},
        {"match": {"user_contains": "DENY_EXEC"},
         "respond": {"capability_call": {
             "capability_id": "execute",
             "arguments": {"language": "sh", "code": "echo should_not_run"},
         }}},
    ])
    task = await _request_execution(svc, "DENY_EXEC do it")
    assert await _collect_up_to(svc, task.id, TaskStatus.WAITING_APPROVAL.value) \
        == TaskStatus.WAITING_APPROVAL.value

    approval_id = await svc.pending_approval_id(task.id)
    assert approval_id is not None

    await svc.approve(approval_id, granted=False)
    final = await svc.wait_for(task.id)
    assert (final.metadata or {}).get("status") == TaskStatus.COMPLETE.value

    # The persisted decision records the denial (BHV-043: no effect executed).
    recs = await svc._store_approvals.list_for_task(task.id)
    assert any(r.get("status") == "DENIED" for r in recs if isinstance(r, dict))

    # And the denied execute never completed on the service event stream.
    events = [e async for e in svc.stream_events(task.id)]
    assert not any(e.type == "CapabilityCompleted" for e in events)