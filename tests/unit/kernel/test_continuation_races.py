"""Deterministic continuation handoff tests.

These tests exercise the boundary around ``_park_wait`` with barriers rather
than wall-clock timing.  The event is intentionally treated as a wakeup hint;
the fake durable stores below decide whether a resume is actually ready.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from athena.concurrency import ReferenceCountedKeyedLocks
from athena.kernel.continuations_coordinator import ContinuationCoordinator
from athena.kernel.kernel import AgentKernel


class _InputReadiness:
    def __init__(self, *, ready: bool = False, gate: asyncio.Event | None = None) -> None:
        self.ready = ready
        self.gate = gate
        self.checks = 0

    async def pending_resumable(self, _task_id: str):
        self.checks += 1
        ready_at_check = self.ready
        if self.gate is not None and self.checks == 1:
            self.gate.set()
            await self.release.wait()
        return {"id": "input-1", "answer": "yes"} if ready_at_check else None

    async def pending_for_task(self, _task_id: str):
        return None

    @property
    def release(self) -> asyncio.Event:
        if self._release is None:
            self._release = asyncio.Event()
        return self._release

    _release: asyncio.Event | None = None


class _ApprovalReadiness:
    def __init__(self, *, ready: bool = False) -> None:
        self.ready = ready
        self.checks = 0

    async def resolved_unconsumed_for_task(self, _task_id: str, *, records: bool = False):
        self.checks += 1
        if records:
            return [{"call_id": "call-1", "decision": "granted"}] if self.ready else []
        return self.ready


def _kernel(task_id: str, *, input_store=None, continuation_store=None):
    kernel = SimpleNamespace(
        _resume={task_id: asyncio.Event()},
        _resume_decision={},
        _resume_armed=set(),
        _resume_locks=ReferenceCountedKeyedLocks(),
        _input_request_store=input_store,
        _continuation_store=continuation_store,
        _parked_slot_wait_s=0.0,
    )
    kernel._arm_resume_wait = lambda current: (
        kernel._resume_armed.add(current),
        kernel._resume[current].clear(),
    )
    return kernel


def _state():
    return SimpleNamespace(cancel=asyncio.Event())


@pytest.mark.asyncio
async def test_answer_already_delivered_before_park_is_consumed_from_durable_state():
    task_id = "task-before-park"
    durable = _InputReadiness(ready=True)
    kernel = _kernel(task_id, input_store=durable)
    kernel._arm_resume_wait(task_id)

    # This is the operator's answer arriving after the durable request exists
    # but before the coordinator reaches its blocking call.
    assert await AgentKernel.notify_input_provided(kernel, task_id, "yes") is True

    result = await ContinuationCoordinator(kernel)._park_wait(SimpleNamespace(id=task_id), _state())
    assert result == "resumed"
    assert durable.checks == 1


@pytest.mark.asyncio
async def test_answer_between_readiness_check_and_wait_is_not_lost():
    task_id = "task-before-visible-state"
    check_started = asyncio.Event()
    durable = _InputReadiness(gate=check_started)
    kernel = _kernel(task_id, input_store=durable)
    kernel._arm_resume_wait(task_id)
    task = SimpleNamespace(id=task_id)

    parked = asyncio.create_task(ContinuationCoordinator(kernel)._park_wait(task, _state()))
    await check_started.wait()

    # notify waits for the coordinator's readiness section to finish. This
    # models an answer arriving in the publication gap without relying on a
    # scheduler sleep.
    notified = asyncio.create_task(AgentKernel.notify_input_provided(kernel, task_id, "yes"))
    durable.ready = True
    durable.release.set()

    assert await notified is True
    assert await parked == "resumed"
    assert durable.checks >= 1


@pytest.mark.asyncio
async def test_timeout_rechecks_durable_answer_at_slot_release_boundary():
    task_id = "task-timeout-boundary"

    class _ArrivingAnswer(_InputReadiness):
        async def pending_resumable(self, _task_id: str):
            self.checks += 1
            # First check says no answer; the second is the mandatory
            # post-timeout authority check.
            self.ready = self.checks > 1
            return {"id": "input-1", "answer": "yes"} if self.ready else None

    durable = _ArrivingAnswer()
    kernel = _kernel(task_id, input_store=durable)
    kernel._arm_resume_wait(task_id)

    result = await ContinuationCoordinator(kernel)._park_wait(SimpleNamespace(id=task_id), _state())
    assert result == "resumed"
    assert durable.checks == 2


@pytest.mark.asyncio
async def test_approval_readiness_is_rechecked_without_event_authority():
    task_id = "task-approval-boundary"
    durable = _ApprovalReadiness(ready=True)
    kernel = _kernel(task_id, continuation_store=durable)
    kernel._arm_resume_wait(task_id)

    result = await ContinuationCoordinator(kernel)._park_wait(SimpleNamespace(id=task_id), _state())
    assert result == "resumed"
    assert durable.checks == 1


@pytest.mark.asyncio
async def test_unanswered_timeout_releases_slot_without_fabricating_resume():
    task_id = "task-slot-release"
    durable = _InputReadiness(ready=False)
    kernel = _kernel(task_id, input_store=durable)
    kernel._arm_resume_wait(task_id)

    result = await ContinuationCoordinator(kernel)._park_wait(SimpleNamespace(id=task_id), _state())
    assert result == "slot_released"
    assert durable.checks == 2
