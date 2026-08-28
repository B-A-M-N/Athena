"""Regression coverage for capability calls through the real dispatcher."""

from __future__ import annotations

import importlib
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

    monkeypatch.setattr(capability, "_fetch", fake_fetch)
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

    monkeypatch.setattr(capability, "_run", fake_run)
    dispatcher = _dispatcher(capability)
    result = await _dispatch(
        dispatcher,
        _request("research", "run", objective="verify release"),
        WorkspaceSpec(id="repo", root=str(tmp_path)),
    )

    assert result.status is CapabilityResultStatus.OK
    assert result.output == "run stub reached"
