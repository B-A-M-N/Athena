"""ACP must preserve resumability and reject malformed authority inputs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from athena.acp.adapter import ACPAdapter, ACPRequest
from athena.protocol.task_codec import TaskCodecError


def _event(sequence: int, event_type: str, payload: dict[str, Any] | None = None):
    return {
        "id": f"evt-{sequence}",
        "type": event_type,
        "sequence": sequence,
        "payload": payload or {},
    }


class SequenceStore:
    def __init__(self, events):
        self._events = [
            SimpleNamespace(
                id=event["id"],
                type=event["type"],
                sequence=event["sequence"],
                payload=event["payload"],
                session_id=None,
            )
            for event in events
        ]

    async def list_for_task(self, _task_id: str, after_sequence: int = 0):
        return [event for event in self._events if event.sequence > after_sequence]


async def _collect(adapter, task_id="task-resume"):
    return [event async for event in adapter.stream(task_id)]


@pytest.mark.asyncio
async def test_interrupted_stream_remains_open_until_resume_completes():
    events = [
        _event(1, "TaskStarted"),
        _event(2, "TaskInterrupted", {"status": "INTERRUPTED"}),
        _event(3, "TaskStarted", {"status": "RUNNING"}),
        _event(4, "TaskCompleted", {"status": "COMPLETE"}),
    ]
    adapter = ACPAdapter(None, None, event_store=SequenceStore(events), stream_poll_interval=0)
    traced = await _collect(adapter)
    assert [event.type for event in traced] == [
        "task.started",
        "task.started",
        "task.message",
        "task.started",
        "task.finished",
    ]
    interrupted = traced[2]
    assert interrupted.payload["athena_event"] == "TaskInterrupted"
    assert interrupted.payload["lifecycle"] == "paused"


@pytest.mark.asyncio
async def test_blocked_and_recovery_streams_remain_open():
    events = [
        _event(1, "TaskStarted"),
        _event(2, "TaskBlocked", {"status": "BLOCKED"}),
        _event(3, "TaskRecoveryRequired", {"status": "RECOVERY_REQUIRED"}),
        _event(4, "TaskCompleted", {"status": "COMPLETE"}),
    ]
    adapter = ACPAdapter(None, None, event_store=SequenceStore(events), stream_poll_interval=0)
    traced = await _collect(adapter)
    assert [event.type for event in traced[2:4]] == ["task.message"] * 2
    assert all(event.payload.get("lifecycle") == "paused" for event in traced[2:4])
    assert traced[-1].type == "task.finished"


@pytest.mark.parametrize(
    ("field", "value"),
    [("autonomy", "offlien"), ("deadline", "not-a-date")],
)
def test_invalid_explicit_input_fails_before_submission(field, value):
    request = ACPRequest(objective="work", **{field: value})
    with pytest.raises(TaskCodecError):
        ACPAdapter(None, None).to_task_spec(request)


def test_omitted_authority_remains_omitted():
    spec = ACPAdapter(None, None).to_task_spec(ACPRequest(objective="work"))
    assert "autonomy" not in spec.metadata
    assert spec.deadline is None
