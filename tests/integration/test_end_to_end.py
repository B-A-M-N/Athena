"""End-to-end cross-subsystem flows through the real AthenaService.

These run the full composition root (TaskManager -> TaskWorker -> AgentKernel ->
real in-memory stores -> background worker / scheduler) against a scripted fake
model. Every test uses a fresh :func:`AthenaService.in_memory` instance.
"""

from __future__ import annotations
import pytest


from athena.protocol.tasks import (
    AgentRequest,
    AutonomyLevel,
    TaskSpec,
    ResourceBudget,
    TaskStatus,
)
from athena.protocol.ids import new_id


async def _status(svc, task_id) -> str | None:
    return await svc.get_task_status(task_id)


@pytest.mark.athena_claim("BHV-024", "BHV-079")
@pytest.mark.athena_evidence("test", "e2e")
async def test_full_loop_returns_complete_with_answer(make_service):
    """start -> submit(wait=True) -> COMPLETE; the model's "4" is streamed."""
    svc = await make_service()
    task = await svc.submit(AgentRequest(prompt="What is 2+2?"), wait=True)
    assert task is not None
    # submit() returns the freshly-created spec; read the live status.
    assert await svc.get_task_status(task.id) == TaskStatus.COMPLETE.value

    result = await svc.get_result(task.id)
    assert result is not None
    assert result.status == TaskStatus.COMPLETE

    deltas = [e for e in await _gather_events(svc, task.id) if e.type == "ModelDelta"]
    assert any("4" in (e.payload or {}).get("text", "") for e in deltas)


@pytest.mark.athena_claim("BHV-016")
@pytest.mark.athena_evidence("test", "e2e")
async def test_non_blocking_submit_runs_via_worker(make_service):
    """Submit with wait=False; the background worker drives it to COMPLETE."""
    svc = await make_service()
    task = await svc.submit(AgentRequest(prompt="What is 2+2?"), wait=False)

    for _ in range(200):
        if (await svc.get_task_status(task.id)) == TaskStatus.COMPLETE.value:
            break
        from asyncio import sleep

        await sleep(0.02)
    assert await svc.get_task_status(task.id) == TaskStatus.COMPLETE.value


@pytest.mark.athena_claim("BHV-076", "BHV-078")
@pytest.mark.athena_evidence("test", "e2e")
async def test_cancel_park_result_is_cancelled(make_service):
    """A task parked on approval can be cancelled -> CANCELLED, not FAILED."""
    svc = await make_service(
        scripts=[
            {
                "match": {"user_contains": "STALL_ME"},
                "respond": {
                    "capability_call": {
                        "capability_id": "execute",
                        "arguments": {"language": "sh", "code": "sleep 30"},
                    }
                },
            },
        ]
    )
    task = await svc.submit(
        AgentRequest(prompt="STALL_ME now", session_id=new_id("session")), wait=False
    )
    for _ in range(150):
        if (await svc.get_task_status(task.id)) == TaskStatus.WAITING_APPROVAL.value:
            break
        from asyncio import sleep

        await sleep(0.02)
    assert await svc.get_task_status(task.id) == TaskStatus.WAITING_APPROVAL.value

    await svc.cancel(task.id, reason="test: active cancel")
    final = await svc.wait_for(task.id)
    assert (final.metadata or {}).get("status") == TaskStatus.CANCELLED.value


@pytest.mark.athena_claim("BHV-134")
@pytest.mark.athena_evidence("test", "e2e")
async def test_budget_exhaustion_yields_partial_not_failed(make_service):
    """A tiny iteration budget ends PARTIAL, never FAILED, after real work."""
    svc = await make_service(
        scripts=[
            {
                "match": {"user_contains": "BUDGET_TICK"},
                "respond": {
                    "capability_call": {
                        "capability_id": "execute",
                        "arguments": {"language": "sh", "code": "echo tick"},
                    }
                },
            },
        ]
    )
    # submit() cannot carry a custom budget (see source note); build the spec
    # with the real stores and let the worker + kernel drive it integration-real.
    spec = TaskSpec(
        id=new_id("task"),
        objective="BUDGET_TICK run",
        session_id=new_id("session"),
        resource_budget=ResourceBudget(max_agent_iterations=2),
        metadata={"autonomy": AutonomyLevel.AUTONOMOUS.value},
    )
    created = await svc._task_manager.create(spec)
    await svc._task_manager.enqueue(created.id)
    final = await svc.wait_for(created.id)
    assert (final.metadata or {}).get("status") == TaskStatus.PARTIAL.value
    result = await svc.get_result(created.id)
    assert result is not None
    assert result.status == TaskStatus.PARTIAL


@pytest.mark.athena_claim("BHV-116")
@pytest.mark.athena_evidence("test", "e2e")
async def test_event_streaming_yields_lifecycle_events(make_service):
    """stream_events over a task yields TaskCreated + iteration + completion."""
    svc = await make_service()
    task = await svc.submit(AgentRequest(prompt="What is 2+2?"), wait=True)
    inspection = await svc.inspect(task.id)
    types = inspection["events"]  # inspect() already reduces events to type names

    assert "TaskCreated" in types
    assert "TaskIterationStarted" in types
    assert "TaskCompleted" in types
    assert len(types) >= 5


async def _gather_events(svc, task_id):
    return [e async for e in svc.stream_events(task_id)]
