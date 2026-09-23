"""Contention evidence for workspace ordering and isolated domains."""

from __future__ import annotations

import asyncio
import math
import time
from types import SimpleNamespace

import pytest

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.registry import CapabilityRegistry
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.reality import ExecutionDisposition
from athena.protocol.tasks import WorkspaceSpec


_DESCRIPTOR = CapabilityDescriptor(
    id="contention.probe",
    description="timed ambiguous execution probe",
    input_schema={"allow_extra": True},
    effects=frozenset({EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
)


class _TimingExecutor:
    descriptor = _DESCRIPTOR

    def __init__(self) -> None:
        self.queue_waits: list[float] = []
        self.execution_times: list[float] = []
        self.active = 0
        self.max_active = 0
        self._lock = asyncio.Lock()

    async def invoke(self, request, *, output_accumulator=None, context=None):
        submitted = float(request.arguments["submitted_at"])
        started = time.perf_counter()
        async with self._lock:
            self.queue_waits.append(started - submitted)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        delay = 0.12 if request.arguments["tag"] == "long" else 0.01
        await asyncio.sleep(delay)
        elapsed = time.perf_counter() - started
        async with self._lock:
            self.execution_times.append(elapsed)
            self.active -= 1
        return CapabilityResult(request.call_id, request.capability_id, CapabilityResultStatus.OK)


class _IsolatedRoute:
    disposition = ExecutionDisposition.ISOLATED

    def __init__(self, workspace):
        self.workspace = workspace

    def metadata(self):
        return {"test_route": "isolated"}


class _IsolatedGate:
    def classify(self, *args, **kwargs):
        return SimpleNamespace(disposition=ExecutionDisposition.ISOLATED)

    async def route(self, request, workspace, effects, descriptor, *, tier=None):
        return _IsolatedRoute(workspace)

    def active_branch(self, task_id):
        return None

    def checkpoint_id(self, task_id):
        return None

    async def discard_ephemeral(self, call_id):
        return None


def _request(tag: str) -> CapabilityRequest:
    return CapabilityRequest(
        capability_id=_DESCRIPTOR.id,
        task_id="contention-task",
        call_id=f"call-{tag}",
        arguments={"code": "probe", "tag": tag, "submitted_at": time.perf_counter()},
    )


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[min(len(ordered) - 1, index)]


async def _measure(dispatcher: CapabilityDispatcher, workspace: WorkspaceSpec):
    executor = next(iter(dispatcher.registry.iter_executors()))
    results = await asyncio.gather(
        dispatcher.dispatch(_request("long"), workspace=workspace),
        dispatcher.dispatch(_request("short"), workspace=workspace),
    )
    assert all(
        isinstance(result, CapabilityResult)
        and result.status is CapabilityResultStatus.OK
        for result in results
    )
    return executor


@pytest.mark.asyncio
async def test_mixed_contention_reports_queue_and_execution_percentiles(tmp_path):
    workspace = WorkspaceSpec(id="contention", root=str(tmp_path))

    serial_executor = _TimingExecutor()
    serial_registry = CapabilityRegistry()
    serial_registry.register(serial_executor)
    serial = CapabilityDispatcher(serial_registry, PolicyEngine("autonomous"))
    serial_result = await _measure(serial, workspace)

    isolated_executor = _TimingExecutor()
    isolated_registry = CapabilityRegistry()
    isolated_registry.register(isolated_executor)
    isolated = CapabilityDispatcher(isolated_registry, PolicyEngine("autonomous"))
    isolated.set_reality_gate(_IsolatedGate())
    isolated_result = await _measure(isolated, workspace)

    serial_queue_p50 = _percentile(serial_result.queue_waits, 0.50)
    serial_queue_p95 = _percentile(serial_result.queue_waits, 0.95)
    isolated_queue_p50 = _percentile(isolated_result.queue_waits, 0.50)
    isolated_queue_p95 = _percentile(isolated_result.queue_waits, 0.95)
    serial_execution_p50 = _percentile(serial_result.execution_times, 0.50)
    serial_execution_p95 = _percentile(serial_result.execution_times, 0.95)
    isolated_execution_p50 = _percentile(isolated_result.execution_times, 0.50)
    isolated_execution_p95 = _percentile(isolated_result.execution_times, 0.95)

    print(
        {
            "serial_queue_p50": serial_queue_p50,
            "serial_queue_p95": serial_queue_p95,
            "serial_execution_p50": serial_execution_p50,
            "serial_execution_p95": serial_execution_p95,
            "isolated_queue_p50": isolated_queue_p50,
            "isolated_queue_p95": isolated_queue_p95,
            "isolated_execution_p50": isolated_execution_p50,
            "isolated_execution_p95": isolated_execution_p95,
        }
    )
    assert serial_result.max_active == 1
    assert isolated_result.max_active == 2
    assert serial_queue_p95 > isolated_queue_p95 + 0.05
    assert serial_execution_p50 > 0.005
    assert isolated_execution_p50 > 0.005
