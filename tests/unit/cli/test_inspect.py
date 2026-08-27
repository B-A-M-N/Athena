"""Tests for `athena inspect` — inference-role observability (audit P0.3).

The INFERENCE section must render which provider/model served each inference
subturn from durable records only (events + the provider-usage store), and a
task with no inference records must render an explicit placeholder instead of
crashing or printing nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pytest

from athena.cli.inspect import run_inspect
from athena.protocol.messages import utcnow


@dataclass
class _Task:
    id: str = "task-1"
    objective: str = "test objective"
    status: str = "completed"
    session_id: str | None = "session-1"
    metadata: dict = field(default_factory=dict)


class _FakeUsageStore:
    def __init__(self, rows: list[dict] | None = None):
        self._rows = list(rows or [])

    async def list_for_task(self, task_id):
        return [dict(r) for r in self._rows if r.get("task_id") == task_id]


@dataclass
class _Service:
    task: _Task
    events: list
    usage_rows: list[dict] = field(default_factory=list)

    @property
    def _provider_usage_store(self):
        return _FakeUsageStore(self.usage_rows)

    async def get_task(self, task_id):
        return self.task if task_id == self.task.id else None

    async def get_result(self, task_id):
        return None

    async def stream_events(self, task_id, after_sequence=0):
        for ev in self.events:
            yield ev


class _Ev:
    """Minimal event stand-in mirroring protocol.events.Event."""

    def __init__(self, type_: str, payload: dict, *, id: str = "evt-1",
                 sequence: int = 1):
        self.id = id
        self.type = type_
        self.sequence = sequence
        self.timestamp = utcnow() or datetime(2026, 1, 1)
        self.task_id = "task-1"
        self.session_id = "session-1"
        self.payload = payload


@pytest.mark.asyncio
async def test_inspect_renders_inference_section_from_events_and_usage(capsys):
    events = [
        _Ev("TaskStarted", {}, id="evt-0", sequence=0),
        _Ev(
            "ModelRequestStarted",
            {"provider": "fake", "model": "fake-standard",
             "provider_profile_id": "fake"},
            id="evt-1", sequence=1,
        ),
        _Ev(
            "ModelResponseCompleted",
            {"provider": "fake", "model": "fake-standard"},
            id="evt-2", sequence=2,
        ),
        _Ev("ModelDelta", {"text": "hello"}, id="evt-3", sequence=3),
    ]
    usage_rows = [
        {
            "id": "usage-1",
            "task_id": "task-1",
            "provider": "fake",
            "model": "fake-standard",
            "input_tokens": 120,
            "output_tokens": 34,
            "cost_usd": "0.0012",
            "started_at": "2026-08-26T00:00:00",
            "metadata": {
                "inference": {"provider_profile_id": "fake"},
                "usage": {"input_tokens": 120, "output_tokens": 34},
            },
        },
        # A failed fallback attempt that never appears in the event stream:
        # must still surface from the durable usage store.
        {
            "id": "usage-2",
            "task_id": "task-1",
            "provider": "broken-provider",
            "model": "broken-model",
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": None,
            "started_at": "2026-08-26T00:00:01",
            "metadata": {},
        },
    ]
    service = _Service(task=_Task(), events=events, usage_rows=usage_rows)

    code = await run_inspect(service, "task-1")
    out = capsys.readouterr().out
    assert code == 0

    # Section header present.
    assert "Inference" in out

    # Event-sourced records: call id, role default, provider/model.
    assert "evt-1" in out
    assert "role=primary" in out
    assert "fake/fake-standard" in out
    assert "completed" in out

    # Usage-store-only record (failed attempt) also rendered.
    assert "usage-2" in out
    assert "broken-provider/broken-model" in out
    assert "recorded" in out


@pytest.mark.asyncio
async def test_inspect_inference_section_explicit_when_no_records(capsys):
    service = _Service(task=_Task(), events=[], usage_rows=[])
    code = await run_inspect(service, "task-1")
    out = capsys.readouterr().out
    assert code == 0
    assert "Inference" in out
    assert "<no inference records>" in out


@pytest.mark.asyncio
async def test_inspect_role_rendered_from_metadata_when_present(capsys):
    # If a future kernel persists the role on the event payload, inspect must
    # surface it rather than forcing the "primary" default.
    events = [
        _Ev(
            "ModelRequestStarted",
            {"provider": "fake", "model": "fake-judge", "role": "judge"},
            id="evt-9", sequence=1,
        ),
    ]
    service = _Service(task=_Task(), events=events, usage_rows=[])
    code = await run_inspect(service, "task-1")
    out = capsys.readouterr().out
    assert code == 0
    assert "role=judge" in out
    assert "fake/fake-judge" in out


@pytest.mark.asyncio
async def test_inspect_survives_service_without_usage_store(capsys):
    # A service with no _provider_usage_store attribute must not crash and
    # still render inference records from events alone.
    class _NoUsageService(_Service):
        @property
        def _provider_usage_store(self):  # pragma: no cover - never reached
            raise AttributeError("no store")

    class _BareService:
        async def get_task(self, task_id):
            return _Task()

        async def get_result(self, task_id):
            return None

        async def stream_events(self, task_id, after_sequence=0):
            yield _Ev(
                "ModelRequestStarted",
                {"provider": "fake", "model": "fake-standard"},
                id="evt-1", sequence=1,
            )

    code = await run_inspect(_BareService(), "task-1")
    out = capsys.readouterr().out
    assert code == 0
    assert "fake/fake-standard" in out
    assert "<no inference records>" not in out
