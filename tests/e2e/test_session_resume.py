"""Session continuity across a service restart using the same DB file.

A follow-up submitted to the same ``session_id`` after the service restarted
must see the prior transcript — the context compiler includes the session's
history, so the resumed task models over the earlier exchange.
"""

from __future__ import annotations
import pytest


from athena.cli.native_session import NativeSession, parse_args
from athena.protocol.tasks import (
    AgentRequest,
    AutonomyLevel,
    TaskStatus,
)
from athena.protocol.ids import new_id


_MARKER = "SENRESUME_MARKER_123"
_FIRST_SCRIPTS = (
    {"match": {"capability_result_ok": True}, "respond": {"text": "", "done": True}},
    {
        "match": {"user_contains": "SEED_TASK"},
        "respond": {
            "capability_call": {
                "capability_id": "execute",
                "arguments": {"language": "python", "code": f"print('{_MARKER}')"},
            }
        },
    },
)
_RESUME_SCRIPTS = (
    # Iteration 1: the resumed context still ends with the seed task's
    # execute result, so the model performs fresh work. Iteration 2: the fs
    # result is now the last capability result, so the generic completion
    # script below takes over. (A bare ``user_contains`` match would fire on
    # iteration 1 too — the seed transcript already contains a capability
    # result — and the task would end without doing anything.)
    {
        "match": {"last_capability_result_contains": _MARKER},
        "respond": {
            "capability_call": {
                "capability_id": "fs",
                "arguments": {"operation": "list", "path": "."},
            }
        },
    },
    {"match": {"capability_result_ok": True}, "respond": {"text": "", "done": True}},
)


async def _wait_terminal(svc, task_id, target=TaskStatus.COMPLETE.value, tries=300, delay=0.02):
    from asyncio import sleep

    for _ in range(tries):
        if (await svc.get_task_status(task_id)) == target:
            return target
        await sleep(delay)
    return await svc.get_task_status(task_id)


@pytest.mark.athena_claim("BHV-026")
@pytest.mark.athena_evidence("test", "e2e")
async def test_resume_session_sees_prior_transcript(make_durable_service, durable_db_path):
    # --- Phase 1: create and complete a task that records a transcript. --- #
    svc1 = await make_durable_service(durable_db_path, scripts=_FIRST_SCRIPTS)
    first = await svc1.submit(
        AgentRequest(
            prompt="SEED_TASK run the marker",
            session_id=new_id("session"),
            autonomy=AutonomyLevel.AUTONOMOUS,
        ),
        wait=False,
    )
    session_id = first.session_id
    assert await _wait_terminal(svc1, first.id) == TaskStatus.COMPLETE.value
    await svc1.stop()

    # --- Phase 2: new service, same DB, resume the session. --------------- #
    # The resumed "continue" turn is tool-eligible under the observable-work
    # gate, so the resumed provider must actually do work — not just emit
    # prose — for the task to finish COMPLETE.
    svc2 = await make_durable_service(durable_db_path, scripts=_RESUME_SCRIPTS)

    # Spy on the model request so we can assert the resumed task's compiled
    # context actually contains the prior session's assistant answer.
    provider = svc2._model_registry.provider_for("fake")
    captured: list[str] = []
    _orig_complete = provider.complete

    async def _spy(request):
        for msg in getattr(request, "messages", ()):
            captured.append(msg.text() or "")
        async for ev in _orig_complete(request):
            yield ev

    provider.complete = _spy

    resumed = await svc2.submit(
        AgentRequest(prompt="continue", session_id=session_id, autonomy=AutonomyLevel.AUTONOMOUS),
        wait=False,
    )
    assert resumed.session_id == session_id
    assert await _wait_terminal(svc2, resumed.id) == TaskStatus.COMPLETE.value

    # --- Phase 3: the resumed task saw the prior transcript in context. --- #
    body = "\n".join(captured)
    assert _MARKER in body, f"prior capability output not compiled into resumed context:\n{body}"

    # Both tasks and the prior capability output are persisted.
    rows = await svc2._db.fetch_all(
        "SELECT id FROM tasks WHERE session_id = ? ORDER BY created_at ASC",
        (session_id,),
    )
    assert len(rows) >= 2, "expected at least two tasks in the same session"

    blocks = []
    for msg in await svc2._store_messages.list_session_messages(session_id):
        blocks.extend(msg.blocks)
    texts = [getattr(b, "output", "") or getattr(b, "text", "") or "" for b in blocks]
    assert any(_MARKER in t for t in texts), "prior transcript not in the session store"


@pytest.mark.athena_claim("BHV-026")
@pytest.mark.athena_evidence("test", "e2e")
@pytest.mark.asyncio
async def test_native_turns_reuse_session_until_new_clears_context(tmp_path):
    """The headless native controller preserves and then clears durable context."""
    from athena.service.service import AthenaService

    first_fact = "NATIVE_DURABLE_FACT_456"
    service = AthenaService.in_memory(
        extra_scripts=[
            {
                "match": {"user_contains": "NATIVE_TURN_ONE"},
                "respond": {"text": first_fact, "done": True},
            },
            {
                "match": {"user_contains": "NATIVE_TURN_TWO"},
                "respond": {"text": "NATIVE_SECOND_REPLY", "done": True},
            },
            {
                "match": {"user_contains": "NATIVE_TURN_THREE"},
                "respond": {"text": "NATIVE_THIRD_REPLY", "done": True},
            },
        ]
    )
    await service.start()
    try:
        session = NativeSession(
            parse_args(["--workspace", str(tmp_path), "--autonomy", "autonomous"])
        )
        session.service = service
        provider = service._model_registry.provider_for("fake")
        original_complete = provider.complete
        captured: list[str] = []

        async def spy(request):
            captured.append("\n".join(msg.text() or "" for msg in request.messages))
            async for event in original_complete(request):
                yield event

        provider.complete = spy

        await session._submit("NATIVE_TURN_ONE store this fact")
        first_session = session.session_id
        assert first_session is not None
        await session._submit("NATIVE_TURN_TWO recall the fact")
        assert session.session_id == first_session
        assert first_fact in captured[-1]

        assert await session._dispatch_command("/new")
        assert session.session_id is None
        captured_before_new_turn = len(captured)
        await session._submit("NATIVE_TURN_THREE do not reuse the old fact")
        assert len(captured) == captured_before_new_turn + 1
        assert first_fact not in captured[-1]
    finally:
        await service.stop()
