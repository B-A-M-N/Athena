"""Session continuity across a service restart using the same DB file.

A follow-up submitted to the same ``session_id`` after the service restarted
must see the prior transcript — the context compiler includes the session's
history, so the resumed task models over the earlier exchange.
"""

from __future__ import annotations
import pytest


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
    svc2 = await make_durable_service(durable_db_path, scripts=None)

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
