"""Operator clarification round-trip: request_input -> WAITING_INPUT -> answer.

The model determines required information is missing, asks via the kernel-
owned ``request_input`` call, and the SAME task resumes with the operator's
answer — no guessing, no question-as-result.
"""

from __future__ import annotations
import pytest

from asyncio import sleep

from athena.protocol.tasks import AgentRequest, AutonomyLevel, TaskStatus
from athena.protocol.ids import new_id


@pytest.mark.athena_claim("BHV-006")
@pytest.mark.athena_evidence("test", "integration")
async def test_request_input_parks_then_resumes_same_task(make_service):
    svc = await make_service(
        scripts=[
            # First turn: ask the operator which file.
            {
                "match": {"last_user_message_contains": "CLARIFY"},
                "respond": {
                    "capability_call": {
                        "capability_id": "request_input",
                        "arguments": {
                            "question": "Which config file should I update?",
                            "choices": ["config/a.yaml", "config/b.yaml"],
                            "expected": "choice",
                        },
                    }
                },
            },
            # After the answer arrives: finish with the answer incorporated.
            {
                "match": {"last_user_message_contains": "config/a.yaml"},
                "respond": {
                    "text": "ANSWERED_WITH: config/a.yaml",
                    "done": True,
                },
            },
        ]
    )

    task = await svc.submit(
        AgentRequest(
            prompt="CLARIFY update my settings",
            session_id=new_id("session"),
            autonomy=AutonomyLevel.AUTONOMOUS,
        ),
        wait=False,
    )

    # The task parks in WAITING_INPUT with the question durable.
    for _ in range(150):
        if (await svc.get_task_status(task.id)) == TaskStatus.WAITING_INPUT.value:
            break
        await sleep(0.02)
    assert await svc.get_task_status(task.id) == TaskStatus.WAITING_INPUT.value

    request = await svc.pending_input(task.id)
    assert request is not None
    assert request["question"] == "Which config file should I update?"
    assert request["choices"] == ["config/a.yaml", "config/b.yaml"]
    assert request["expected"] == "choice"
    assert request["status"] == "OPEN"

    # The operator answers; the SAME task resumes and completes.
    await svc.provide_input(task.id, "config/a.yaml")
    final = await svc.wait_for(task.id)
    assert (final.metadata or {}).get("status") == TaskStatus.COMPLETE.value

    # The request records the answer.
    resolved = await svc._store_input_requests.pending_for_task(task.id)
    assert resolved is None

    # The answer is in the session transcript as provenance.
    messages = await svc._store_messages.list_session_messages(task.session_id)
    assert any(
        "config/a.yaml" in (block.text if hasattr(block, "text") else "")
        for message in messages
        for block in message.blocks
    )

    result = await svc.get_result(task.id)
    assert result is not None
    assert "ANSWERED_WITH: config/a.yaml" in result.summary


@pytest.mark.athena_claim("BHV-006")
@pytest.mark.athena_evidence("test", "integration")
async def test_request_input_survives_open_request_listing(make_service):
    svc = await make_service(
        scripts=[
            {
                "match": {"last_user_message_contains": "NEED_INFO"},
                "respond": {
                    "capability_call": {
                        "capability_id": "request_input",
                        "arguments": {"question": "Which machine?"},
                    }
                },
            },
            {
                "match": {"last_user_message_contains": "machine-x"},
                "respond": {"text": "deployed to machine-x", "done": True},
            },
        ]
    )

    session = new_id("session")
    task = await svc.submit(
        AgentRequest(
            prompt="NEED_INFO deploy",
            session_id=session,
            autonomy=AutonomyLevel.AUTONOMOUS,
        ),
        wait=False,
    )
    for _ in range(150):
        if (await svc.get_task_status(task.id)) == TaskStatus.WAITING_INPUT.value:
            break
        await sleep(0.02)

    open_requests = await svc._store_input_requests.list_open(session_id=session)
    assert len(open_requests) == 1
    assert open_requests[0]["task_id"] == task.id
    assert open_requests[0]["question"] == "Which machine?"

    await svc.provide_input(task.id, "machine-x")
    await svc.wait_for(task.id)
    assert await svc.get_task_status(task.id) == TaskStatus.COMPLETE.value
    assert await svc._store_input_requests.list_open(session_id=session) == []
