"""Worker slot release for parked tasks (P1-17).

A parked task (WAITING_INPUT / WAITING_APPROVAL) must not pin its worker
coroutine for the hours an operator may take. Past the slot deadline the
run returns with the task left in its paused status; the operator's
action relaunches the SAME task, and the relaunched loop consumes the
durable state before its first model call.
"""

from __future__ import annotations

from asyncio import sleep

import pytest

from athena.protocol.tasks import AgentRequest, AutonomyLevel, TaskStatus
from athena.service.config import AthenaConfig, ProviderConfig
from athena.service.service import AthenaService


async def _wait_status(svc, task_id, status, attempts: int = 200):
    for _ in range(attempts):
        current = await svc.get_task_status(task_id)
        if current == status.value:
            return True
        await sleep(0.02)
    return False


def _config(scripts, parked_slot_wait_s: float = 1.0) -> AthenaConfig:
    import os
    import tempfile

    tmp = tempfile.mkdtemp(prefix="athena-slot-")
    return AthenaConfig(
        db_path=":memory:",
        workspace_root=tmp,
        artifact_root=os.path.join(tmp, "artifacts"),
        providers=(
            ProviderConfig(kind="fake", name="fake", extra={"scripts": scripts}),
        ),
        parked_slot_wait_s=parked_slot_wait_s,
    )


ASK_SCRIPT = {
    "match": {"last_user_message_contains": "CLARIFY"},
    "respond": {
        "capability_call": {
            "capability_id": "request_input",
            "arguments": {
                "question": "Which config file?",
                "choices": ["a", "b"],
            },
        }
    },
}
ANSWER_SCRIPT = {
    "match": {"last_user_message_contains": "a"},
    "respond": {"text": "ANSWERED: a", "done": True},
}


@pytest.mark.asyncio
async def test_parked_input_releases_worker_then_relaunches():
    """Slot deadline fires while no operator has answered; the task stays
    WAITING_INPUT (not terminal); the operator's answer relaunches the SAME
    task and it completes."""
    svc = AthenaService(config=_config([ASK_SCRIPT, ANSWER_SCRIPT]))
    await svc.start()
    try:
        task = await svc.submit(
            AgentRequest(
                prompt="CLARIFY choose one",
                session_id="s-slot-1",
                autonomy=AutonomyLevel.AUTONOMOUS,
            ),
            wait=False,
        )
        # Parks in WAITING_INPUT...
        assert await _wait_status(svc, task.id, TaskStatus.WAITING_INPUT)
        # ...and the slot deadline releases the run. The status REMAINS
        # WAITING_INPUT (paused, non-terminal) after the deadline.
        await sleep(1.6)
        assert await svc.get_task_status(task.id) == TaskStatus.WAITING_INPUT.value

        # The operator answers with no live coroutine; relaunch completes it.
        await svc.provide_input(task.id, "a")
        deadline = 20.0
        waited = 0.0
        while waited < deadline:
            if await svc.get_task_status(task.id) == TaskStatus.COMPLETE.value:
                break
            await sleep(0.1)
            waited += 0.1
        assert await svc.get_task_status(task.id) == TaskStatus.COMPLETE.value
        result = await svc.get_result(task.id)
        assert result is not None and "ANSWERED: a" in result.summary
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_answer_landing_inside_deadline_window_is_consumed():
    """An answer that lands between park and slot deadline is consumed by
    the dying run's re-check (not stranded until a relaunch)."""
    answer_arrived = None

    svc = AthenaService(
        config=_config(
            [
                ASK_SCRIPT,
                {
                    "match": {"last_user_message_contains": "b"},
                    "respond": {"text": "ANSWERED: b", "done": True},
                },
            ],
            parked_slot_wait_s=5.0,
        )
    )
    await svc.start()
    try:
        task = await svc.submit(
            AgentRequest(
                prompt="CLARIFY choose one",
                session_id="s-slot-2",
                autonomy=AutonomyLevel.AUTONOMOUS,
            ),
            wait=False,
        )
        assert await _wait_status(svc, task.id, TaskStatus.WAITING_INPUT)
        # Answer while the live run is still parked (deadline not fired).
        await svc.provide_input(task.id, "b")
        waited = 0.0
        while waited < 15.0:
            if await svc.get_task_status(task.id) == TaskStatus.COMPLETE.value:
                answer_arrived = True
                break
            await sleep(0.1)
            waited += 0.1
        assert answer_arrived, "answer inside the window must complete the task"
    finally:
        await svc.stop()
