"""Regression coverage for capability calls through the real dispatcher."""

from __future__ import annotations

import importlib
import pytest
import inspect
import pkgutil

import athena.capabilities as capabilities_package
from athena.capabilities.delegate import DelegateCapability
from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.memory import MemoryCapability
from athena.capabilities.registry import CapabilityRegistry
from athena.capabilities.research import ResearchCapability
from athena.capabilities.schedule import ScheduleCapability
from athena.capabilities.skills import SkillsCapability
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResult,
    CapabilityResultStatus,
)
from athena.protocol.tasks import AutonomyLevel, TaskResult, TaskStatus, WorkspaceSpec


def _request(capability_id: str, operation: str, **arguments) -> CapabilityRequest:
    return CapabilityRequest(
        capability_id=capability_id,
        task_id="task-dispatch-contracts",
        call_id=f"{capability_id}-{operation}",
        origin=CapabilityRequestOrigin.MODEL,
        arguments={"operation": operation, **arguments},
    )


def _dispatcher(*executors, profile: AutonomyLevel = AutonomyLevel.CODING) -> CapabilityDispatcher:
    registry = CapabilityRegistry()
    for executor in executors:
        registry.register(executor)
    return CapabilityDispatcher(registry, PolicyEngine(profile))


async def _dispatch(dispatcher, request, workspace):
    return await dispatcher.dispatch(request, workspace=workspace)


class _MemoryStore:
    async def recall(self, *, query, tags, **kwargs):
        return [{"query": query, "tags": tags, "content": "remembered"}]

    async def search(self, *, query, limit, **kwargs):
        return [{"query": query, "limit": limit, "content": "remembered"}]


class _SkillsStore:
    async def search(self, *, query, limit=10):
        return [{"id": "skill-1", "query": query}]

    async def trigger(self, *, skill_id, arguments, task_id=None):
        return {"id": skill_id, "arguments": arguments, "task_id": task_id}


class _ScheduleAPI:
    async def list_jobs(self, **kwargs):
        return [{"id": "job-1", "name": "nightly", "enabled": True}]

    async def inspect(self, job_id, **kwargs):
        return {"id": job_id, "name": "nightly", "enabled": True}


class _DelegationHandle:
    async def is_descendant(self, parent_task_id, child_task_id):
        return parent_task_id == "task-dispatch-contracts" and child_task_id == "child-1"

    async def status_of(self, child_task_id):
        return TaskStatus.COMPLETE

    async def collect(self, child_task_id, *, timeout=None):
        return TaskResult(child_task_id, TaskStatus.COMPLETE, summary="child finished")


class _PendingDelegationHandle(_DelegationHandle):
    async def collect(self, child_task_id, *, timeout=None):
        return TaskResult(child_task_id, TaskStatus.WAITING_APPROVAL)


async def test_memory_and_skills_accept_dispatcher_context(tmp_path):
    dispatcher = _dispatcher(
        MemoryCapability(_MemoryStore()),
        SkillsCapability(_SkillsStore()),
        profile=AutonomyLevel.SUPERVISED,
    )
    workspace = WorkspaceSpec(id="repo", root=str(tmp_path))

    memory = await _dispatch(dispatcher, _request("memory", "recall", query="remember"), workspace)
    skills = await _dispatch(dispatcher, _request("skills", "search", query="python"), workspace)

    assert memory.status is CapabilityResultStatus.OK
    assert skills.status is CapabilityResultStatus.OK


async def test_schedule_read_operations_fit_descriptor_effect_envelope(tmp_path):
    dispatcher = _dispatcher(ScheduleCapability(_ScheduleAPI()), profile=AutonomyLevel.SUPERVISED)
    workspace = WorkspaceSpec(id="repo", root=str(tmp_path))

    listed = await _dispatch(dispatcher, _request("schedule", "list"), workspace)
    inspected = await _dispatch(
        dispatcher, _request("schedule", "inspect", job_id="job-1"), workspace
    )

    assert listed.status is CapabilityResultStatus.OK
    assert inspected.status is CapabilityResultStatus.OK


async def test_delegate_read_operations_fit_descriptor_effect_envelope(tmp_path):
    dispatcher = _dispatcher(
        DelegateCapability(_DelegationHandle()), profile=AutonomyLevel.SUPERVISED
    )
    workspace = WorkspaceSpec(id="repo", root=str(tmp_path))

    status = await _dispatch(
        dispatcher,
        _request("delegate", "status", child_task_id="child-1"),
        workspace,
    )
    collected = await _dispatch(
        dispatcher,
        _request("delegate", "collect", child_task_id="child-1"),
        workspace,
    )

    assert status.status is CapabilityResultStatus.OK
    assert "COMPLETE" in status.output
    assert collected.status is CapabilityResultStatus.OK
    assert "child finished" in collected.output


async def test_delegate_rejects_child_outside_requesting_subtree(tmp_path):
    dispatcher = _dispatcher(
        DelegateCapability(_DelegationHandle()), profile=AutonomyLevel.SUPERVISED
    )
    result = await _dispatch(
        dispatcher,
        _request("delegate", "status", child_task_id="other-child"),
        WorkspaceSpec(id="repo", root=str(tmp_path)),
    )
    assert result.status is CapabilityResultStatus.FAILED
    assert "not owned" in (result.error or "")


async def test_delegate_collect_does_not_report_pending_as_success(tmp_path):
    dispatcher = _dispatcher(
        DelegateCapability(_PendingDelegationHandle()), profile=AutonomyLevel.SUPERVISED
    )
    result = await _dispatch(
        dispatcher,
        _request("delegate", "collect", child_task_id="child-1", timeout=0),
        WorkspaceSpec(id="repo", root=str(tmp_path)),
    )
    assert result.status is CapabilityResultStatus.FAILED
    assert "not complete" in (result.error or "")


async def test_research_fetch_reaches_executor_after_effect_resolution(tmp_path, monkeypatch):
    capability = ResearchCapability(store=object())

    async def fake_fetch(request, args, context):
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.OK,
            output="fetch stub reached",
        )

    monkeypatch.setattr(capability._service, "_fetch", fake_fetch)
    dispatcher = _dispatcher(capability)
    result = await _dispatch(
        dispatcher,
        _request("research", "fetch", uri="https://example.com/source"),
        WorkspaceSpec(id="repo", root=str(tmp_path)),
    )

    assert result.status is CapabilityResultStatus.OK
    assert result.output == "fetch stub reached"


def test_native_operation_schemas_and_static_effect_maps_cannot_drift():
    """Every declared static operation must have exactly one effect contract."""
    modules = [
        importlib.import_module(module.name)
        for module in pkgutil.iter_modules(
            capabilities_package.__path__, capabilities_package.__name__ + "."
        )
    ]
    descriptors: dict[str, CapabilityDescriptor] = {}
    for module in modules:
        for _, cls in inspect.getmembers(module, inspect.isclass):
            descriptor = getattr(cls, "descriptor", None)
            if isinstance(descriptor, CapabilityDescriptor):
                descriptors[descriptor.id] = descriptor

    mismatches = []
    for descriptor in descriptors.values():
        operations = set(
            descriptor.input_schema.get("properties", {}).get("operation", {}).get("enum", ())
        )
        if not operations or descriptor.operation_effects is None:
            continue
        declared = set(descriptor.operation_effects)
        missing = sorted(operations - declared)
        extra = sorted(declared - operations)
        if missing or extra:
            mismatches.append(f"{descriptor.id}: missing={missing!r}, extra={extra!r}")

    assert not mismatches, "operation/effect contract drift: " + "; ".join(mismatches)


async def test_research_run_reaches_executor_after_effect_resolution(tmp_path, monkeypatch):
    capability = ResearchCapability(store=object())

    async def fake_run(request, args, context):
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.OK,
            output="run stub reached",
        )

    monkeypatch.setattr(capability._service, "_run", fake_run)
    dispatcher = _dispatcher(capability)
    result = await _dispatch(
        dispatcher,
        _request("research", "run", objective="verify release"),
        WorkspaceSpec(id="repo", root=str(tmp_path)),
    )

    assert result.status is CapabilityResultStatus.OK
    assert result.output == "run stub reached"


@pytest.mark.asyncio
async def test_resource_key_resolver_on_descriptor_is_used_for_locks():
    """A descriptor-level resource_key_resolver replaces path/destination
    guessing and lets future filesystem-like capabilities declare exact
    resource identity without dispatcher changes."""

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
    from athena.protocol.tasks import AutonomyLevel, WorkspaceSpec

    class E:
        descriptor = CapabilityDescriptor(
            id="custom.sync",
            description="sync two declared resources",
            input_schema={"type": "object"},
            effects=frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
            resource_key_resolver=lambda args, ws: ("alpha", "beta"),
        )

        async def invoke(self, request, *, output_accumulator=None, context=None):
            return CapabilityResult(
                request.call_id, request.capability_id, CapabilityResultStatus.OK
            )

    ws = WorkspaceSpec(id="repo", root="/tmp")
    reg = CapabilityRegistry()
    reg.register(E())
    d = CapabilityDispatcher(reg, PolicyEngine(AutonomyLevel.AUTONOMOUS))
    r = CapabilityRequest("custom.sync", {}, task_id="t", call_id="c1")
    locks = d._locks_for_request(r, ws, (EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL))
    assert len(locks) == 2
    keys = sorted(d._resource_locks.keys())
    assert len(keys) == 2


@pytest.mark.asyncio
async def test_resource_locks_are_pruned_after_release():

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
    from athena.protocol.tasks import AutonomyLevel, WorkspaceSpec

    class W:
        descriptor = CapabilityDescriptor(
            id="fs.write",
            description="w",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            effects=frozenset({EffectClass.WRITE_LOCAL}),
        )

        async def invoke(self, request, *, output_accumulator=None, context=None):
            return CapabilityResult(
                request.call_id, request.capability_id, CapabilityResultStatus.OK
            )

    ws = WorkspaceSpec(id="repo", root="/tmp")
    reg = CapabilityRegistry()
    reg.register(W())
    d = CapabilityDispatcher(reg, PolicyEngine(AutonomyLevel.AUTONOMOUS))

    r1 = CapabilityRequest("fs.write", {"path": "a.txt"}, task_id="t", call_id="c1")
    r2 = CapabilityRequest("fs.write", {"path": "b.txt"}, task_id="t", call_id="c2")
    locks1 = d._locks_for_request(r1, ws, (EffectClass.WRITE_LOCAL,))
    assert len(d._resource_locks) == 1
    locks2 = d._locks_for_request(r2, ws, (EffectClass.WRITE_LOCAL,))
    assert len(d._resource_locks) == 2

    # Acquire both, then release with no contention — both entries pruned
    for lock in locks1:
        await lock.acquire()
    for lock in locks2:
        await lock.acquire()
    d._release_locks(locks1)
    d._release_locks(locks2)
    assert len(d._resource_locks) == 0


@pytest.mark.asyncio
async def test_resource_lock_reference_holds_across_waiter_and_releaser():
    """A waiting caller keeps the keyed lock identity while another releases.

    Regression for the historical race:
      B obtains lock L but has not awaited it;
      A releases L and the old implementation pruned it because _waiters
      was not populated until await;
      C created a new L2 for the same key.
    B and C could then acquire different locks concurrently.
    """
    import asyncio

    from athena.capabilities.dispatcher import CapabilityDispatcher
    from athena.capabilities.registry import CapabilityRegistry
    from athena.policy.engine import PolicyEngine
    from athena.protocol.capabilities import (
        CapabilityRequest,
        CapabilityResult,
        CapabilityResultStatus,
        EffectClass,
    )
    from athena.capabilities.operations import native_descriptor
    from athena.protocol.tasks import AutonomyLevel, WorkspaceSpec

    class W:
        descriptor = native_descriptor(
            id="fs.write",
            description="w",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            effects=frozenset({EffectClass.WRITE_LOCAL}),
        )

        async def invoke(self, request, *, output_accumulator=None, context=None):
            return CapabilityResult(
                request.call_id, request.capability_id, CapabilityResultStatus.OK
            )

    ws = WorkspaceSpec(id="repo", root="/tmp")
    reg = CapabilityRegistry()
    reg.register(W())
    d = CapabilityDispatcher(reg, PolicyEngine(AutonomyLevel.AUTONOMOUS))
    request = CapabilityRequest("fs.write", {"path": "same.txt"}, task_id="t", call_id="c")

    # A currently holds the keyed lock.
    lock_a = d._locks_for_request(request, ws, (EffectClass.WRITE_LOCAL,))[0]
    await lock_a.acquire()

    # B takes a reference before it actually awaits the lock.
    lock_b = d._locks_for_request(request, ws, (EffectClass.WRITE_LOCAL,))[0]
    waiter = asyncio.create_task(lock_b.acquire())
    await asyncio.sleep(0)
    assert not waiter.done()

    # A releases its reference. The entry must survive B's outstanding reference.
    d._release_locks([lock_a])
    lock_c = d._locks_for_request(request, ws, (EffectClass.WRITE_LOCAL,))[0]
    assert lock_c is lock_b

    await waiter
    d._release_locks([lock_b])
    d._resource_locks.release_reference(lock_b)
    assert len(d._resource_locks) == 0


@pytest.mark.asyncio
async def test_ambient_order_lane_is_global_across_batches():
    """Two concurrent dispatch_many batches of resource-less writes must
    serialize on the SAME dispatcher-owned lane (item 5), not two unrelated
    per-batch locks."""
    import asyncio

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
    from athena.protocol.tasks import AutonomyLevel, WorkspaceSpec

    class W:
        descriptor = CapabilityDescriptor(
            id="execute.ambient",
            description="b",
            input_schema={"type": "object"},
            effects=frozenset({EffectClass.EXECUTE}),
        )

        def __init__(self):
            self.active = 0
            self.max_active = 0

        async def invoke(self, request, *, output_accumulator=None, context=None):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.1)
            self.active -= 1
            return CapabilityResult(
                request.call_id, request.capability_id, CapabilityResultStatus.OK
            )

    ws = WorkspaceSpec(id="repo", root="/tmp")
    reg = CapabilityRegistry()
    ex = W()
    reg.register(ex)
    d = CapabilityDispatcher(reg, PolicyEngine(AutonomyLevel.AUTONOMOUS))

    def req(i):
        return CapabilityRequest("execute.ambient", {}, task_id="t", call_id=f"c{i}")

    batch_a = [req(1), req(2)]
    batch_b = [req(3), req(4)]
    await asyncio.gather(
        d.dispatch_many(batch_a, workspace=ws),
        d.dispatch_many(batch_b, workspace=ws),
    )
    assert ex.max_active == 1, f"cross-batch ambient writes raced: {ex.max_active}"


@pytest.mark.asyncio
async def test_prepared_call_resolves_executor_once_per_dispatch():
    """Item 4: the ordering layer's prepared call is consumed by the core —
    executor resolution happens once per dispatch, not twice."""
    import asyncio

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
    from athena.protocol.tasks import AutonomyLevel, WorkspaceSpec

    resolutions = {"n": 0}

    class CountingRegistry(CapabilityRegistry):
        def executor_for(self, capability_id):
            resolutions["n"] += 1
            return super().executor_for(capability_id)

    class E:
        descriptor = CapabilityDescriptor(
            id="fs.read",
            description="r",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            effects=frozenset({EffectClass.READ_LOCAL}),
        )

        async def invoke(self, request, *, output_accumulator=None, context=None):
            return CapabilityResult(
                request.call_id, request.capability_id, CapabilityResultStatus.OK
            )

    ws = WorkspaceSpec(id="repo", root="/tmp")
    reg = CountingRegistry()
    reg.register(E())
    d = CapabilityDispatcher(reg, PolicyEngine(AutonomyLevel.AUTONOMOUS))
    result = await asyncio.wait_for(
        d.dispatch(
            CapabilityRequest("fs.read", {"path": "x"}, task_id="t", call_id="c1"), workspace=ws
        ),
        10,
    )
    assert result.status is CapabilityResultStatus.OK
    # Controls prepare (1) + any internal mediated path must not double-resolve.
    assert resolutions["n"] == 1, f"executor resolved {resolutions['n']} times"
