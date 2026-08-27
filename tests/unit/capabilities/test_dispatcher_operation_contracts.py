"""Regression coverage for capability calls through the real dispatcher."""

from __future__ import annotations

from athena.capabilities.delegate import DelegateCapability
from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.memory import MemoryCapability
from athena.capabilities.registry import CapabilityRegistry
from athena.capabilities.research import ResearchCapability
from athena.capabilities.schedule import ScheduleCapability
from athena.capabilities.skills import SkillsCapability
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
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


def _dispatcher(
    *executors, profile: AutonomyLevel = AutonomyLevel.CODING
) -> CapabilityDispatcher:
    registry = CapabilityRegistry()
    for executor in executors:
        registry.register(executor)
    return CapabilityDispatcher(registry, PolicyEngine(profile))


async def _dispatch(dispatcher, request, workspace):
    return await dispatcher.dispatch(request, workspace=workspace)


class _MemoryStore:
    async def recall(self, *, query, tags):
        return [{"query": query, "tags": tags, "content": "remembered"}]


class _SkillsStore:
    async def search(self, *, query):
        return [{"id": "skill-1", "query": query}]


class _ScheduleAPI:
    async def list_jobs(self):
        return [{"id": "job-1", "name": "nightly", "enabled": True}]

    async def inspect(self, job_id):
        return {"id": job_id, "name": "nightly", "enabled": True}


class _DelegationHandle:
    async def status_of(self, child_task_id):
        return TaskStatus.COMPLETE

    async def collect(self, child_task_id, *, timeout=None):
        return TaskResult(child_task_id, TaskStatus.COMPLETE, summary="child finished")


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
    dispatcher = _dispatcher(
        ScheduleCapability(_ScheduleAPI()), profile=AutonomyLevel.SUPERVISED
    )
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


async def test_research_fetch_reaches_executor_after_effect_resolution(
    tmp_path, monkeypatch
):
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
