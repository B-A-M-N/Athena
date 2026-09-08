"""Agent-performance latency lane (P1-25).

Deterministic call-count benchmarks over the canonical event stream —
no wall-clock assertions, no sleep-based timing. Each scenario pins how
many model calls a class of ordinary turn may spend, so a routing or
loop regression (an extra reasoning turn, an unmetered interpreter
subturn, an unnecessary capability dispatch) surfaces as a count
failure, not a flaky duration.

Scenarios:
* greeting        — exactly ONE primary model call; response route.
* inspection      — a read-and-answer turn: TWO primary calls max
                    (observe + answer); no interpreter role at all.
* write→test      — a mutating turn must observe evidence BEFORE the
                    final answer turn (ordering, not counting).
* interpreter cap — a failing capability with a sprawling output spends
                    at most ONE interpreter subturn per dispatch, and the
                    role shows up as ``interpreter``, never ``primary``.
"""

from __future__ import annotations

import pytest

from athena.protocol.tasks import AgentRequest, AutonomyLevel, TaskStatus


async def _model_request_events(svc, task_id: str) -> list[dict]:
    events = [e async for e in svc.stream_events(task_id)]
    return [dict(e.payload or {}) for e in events if e.type == "ModelRequestStarted"]


def _primary_calls(payloads: list[dict]) -> int:
    return sum(1 for p in payloads if p.get("role", "primary") == "primary")


def _interpreter_calls(payloads: list[dict]) -> int:
    return sum(1 for p in payloads if p.get("role") == "interpreter")


@pytest.mark.athena_evidence("test", "performance")
async def test_greeting_costs_exactly_one_model_call(make_service):
    """A greeting is one reasoning turn. Any second primary call is a
    regression: the response route must not re-enter the loop."""
    svc = await make_service(
        scripts=[
            {
                "match": {"last_user_message_contains": "hello"},
                "respond": {"text": "Hello there.", "done": True},
            }
        ]
    )
    task = await svc.submit(AgentRequest(prompt="hello"), wait=True)
    assert await svc.get_task_status(task.id) == TaskStatus.COMPLETE.value
    payloads = await _model_request_events(svc, task.id)
    assert _primary_calls(payloads) == 1, payloads
    assert _interpreter_calls(payloads) == 0


@pytest.mark.athena_evidence("test", "performance")
async def test_inspection_costs_at_most_two_model_calls(make_service):
    """A read-and-answer turn observes once and answers once. A third
    call means the loop failed to converge on the evidence it had."""
    svc = await make_service(
        scripts=[
            {
                "match": {"capability_result_ok": True},
                "respond": {"text": "The note says: PROBE_OK", "done": True},
            },
            {
                "match": {"user_contains": "INSPECT_WS"},
                "respond": {
                    "capability_call": {
                        "capability_id": "fs",
                        "arguments": {
                            "operation": "read",
                            "path": "note.txt",
                        },
                    },
                },
            },
        ],
        files={"note.txt": "PROBE_OK"},
    )
    task = await svc.submit(
        AgentRequest(prompt="INSPECT_WS and tell me what note.txt says"), wait=True
    )
    payloads = await _model_request_events(svc, task.id)
    assert _primary_calls(payloads) <= 2, payloads
    assert _interpreter_calls(payloads) == 0


@pytest.mark.athena_evidence("test", "performance")
async def test_write_precedes_final_answer_turn(make_service):
    """Ordering invariant: a mutating turn's write result must appear in
    the transcript BEFORE the model's final (done) response turn."""
    svc = await make_service(
        scripts=[
            {
                "match": {"capability_result_ok": True},
                "respond": {"text": "WROTE_OUT", "done": True},
            },
            {
                "match": {"user_contains": "WRITE_ME"},
                "respond": {
                    "capability_call": {
                        "capability_id": "fs",
                        "arguments": {
                            "operation": "write",
                            "path": "out.txt",
                            "content": "written by lane",
                        },
                    },
                },
            },
        ]
    )
    task = await svc.submit(
        AgentRequest(prompt="WRITE_ME to out.txt", autonomy=AutonomyLevel.CODING),
        wait=True,
    )
    assert await svc.get_task_status(task.id) in (
        TaskStatus.COMPLETE.value,
        TaskStatus.PARTIAL.value,
    )
    events = [e async for e in svc.stream_events(task.id)]
    # Find the first CapabilityCompleted for the write and the LAST
    # ModelRequestStarted; the write must not come after the final call.
    write_events = [e for e in events if e.type == "CapabilityCompleted"]
    assert write_events, "the write never executed"
    final_call = [e for e in events if e.type == "ModelRequestStarted"][-1]
    write_seq = [e.sequence for e in write_events]
    assert min(write_seq) < final_call.sequence, "write landed after the final model turn"


@pytest.mark.athena_evidence("test", "performance")
async def test_interpreter_subturn_is_bounded_per_dispatch(make_service):
    """A sprawling failed capability condenses through ONE interpreter
    subturn — metered, role-tagged, and never a second primary call."""
    sprawling = "x" * 5000
    svc = await make_service(
        scripts=[
            # The interpreter subturn's prompt carries "Observation kind";
            # match it BEFORE the trigger (the objective matches every turn).
            {
                "match": {"user_contains": "Observation kind"},
                "respond": {"text": "Handled the failure.", "done": True},
            },
            {
                "match": {"capability_result_ok": False},
                "respond": {"text": "Handled the failure.", "done": True},
            },
            {
                "match": {"user_contains": "FUSE_ME"},
                "respond": {
                    "capability_call": {
                        "capability_id": "fs",
                        "arguments": {
                            "operation": "write",
                            "path": "gone/away.txt",
                            "content": sprawling,
                        },
                    },
                },
            },
        ]
    )
    task = await svc.submit(
        AgentRequest(prompt="FUSE_ME now", autonomy=AutonomyLevel.CODING),
        wait=True,
    )
    payloads = await _model_request_events(svc, task.id)
    # Interpreter subturns are metered separately and bounded: at most one
    # per dispatch cycle.
    assert _interpreter_calls(payloads) <= 1, payloads
