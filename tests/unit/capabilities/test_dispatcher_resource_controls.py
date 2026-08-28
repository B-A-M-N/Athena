"""Dispatcher-path coverage for task concurrency and resource controls."""

from __future__ import annotations

import asyncio
from collections import defaultdict

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.fs import FilesystemCapability
from athena.capabilities.registry import CapabilityRegistry
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
    InvocationContext,
)
from athena.protocol.tasks import ResourceBudget, WorkspaceSpec


class _SlowExecutor:
    def __init__(self, descriptor: CapabilityDescriptor) -> None:
        self.descriptor = descriptor
        self.invocations = []
        self.active = 0
        self.max_active = 0
        self.active_by_path: defaultdict[str, int] = defaultdict(int)
        self.max_by_path: defaultdict[str, int] = defaultdict(int)
        self.seen_budgets: list[ResourceBudget | None] = []

    async def invoke(self, request, *, context: InvocationContext | None = None, **_):
        self.invocations.append(request)
        self.seen_budgets.append(getattr(context, "resource_budget", None))
        path = str(request.arguments.get("path") or "")
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.active_by_path[path] += 1
        self.max_by_path[path] = max(self.max_by_path[path], self.active_by_path[path])
        try:
            await asyncio.sleep(0.02)
        finally:
            self.active -= 1
            self.active_by_path[path] -= 1
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.OK,
            output="ok",
        )


def _request(capability_id: str, call_id: str, **arguments) -> CapabilityRequest:
    return CapabilityRequest(
        capability_id=capability_id,
        arguments=arguments,
        task_id="task-resource-controls",
        call_id=call_id,
    )


def _workspace(tmp_path) -> WorkspaceSpec:
    return WorkspaceSpec(id="resource-controls", root=str(tmp_path))


async def test_dispatch_many_caps_execution_concurrency(tmp_path):
    executor = _SlowExecutor(
        CapabilityDescriptor(
            id="slow-execute",
            description="test execution capability",
            input_schema={"type": "object", "additionalProperties": True},
            effects=frozenset({EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
        )
    )
    registry = CapabilityRegistry()
    registry.register(executor)
    dispatcher = CapabilityDispatcher(registry, PolicyEngine("autonomous"))
    budget = ResourceBudget(max_parallel_executions=2)

    results = await dispatcher.dispatch_many(
        [_request("slow-execute", f"call-{i}") for i in range(6)],
        workspace=_workspace(tmp_path),
        task_budget=budget,
    )

    assert [result.status for result in results] == [CapabilityResultStatus.OK] * 6
    assert executor.max_active == 2
    assert executor.seen_budgets == [budget] * 6


async def test_dispatch_many_serializes_conflicting_paths_but_allows_independent_paths(
    tmp_path,
):
    executor = _SlowExecutor(
        CapabilityDescriptor(
            id="slow-files",
            description="test local mutation capability",
            input_schema={
                "type": "object",
                "required": ["operation", "path"],
                "properties": {
                    "operation": {"const": "write"},
                    "path": {"type": "string"},
                },
                "additionalProperties": False,
            },
            effects=frozenset({EffectClass.WRITE_LOCAL}),
        )
    )
    registry = CapabilityRegistry()
    registry.register(executor)
    dispatcher = CapabilityDispatcher(registry, PolicyEngine("autonomous"))

    results = await dispatcher.dispatch_many(
        [
            _request("slow-files", "same-1", operation="write", path="same.txt"),
            _request("slow-files", "same-2", operation="write", path="same.txt"),
            _request("slow-files", "other", operation="write", path="other.txt"),
        ],
        workspace=_workspace(tmp_path),
        task_budget=ResourceBudget(max_parallel_executions=4),
    )

    assert [result.status for result in results] == [CapabilityResultStatus.OK] * 3
    assert executor.max_by_path["same.txt"] == 1
    assert executor.max_active == 2


async def test_successful_reads_are_cached_until_a_mutation_invalidates_them(tmp_path):
    read_executor = _SlowExecutor(
        CapabilityDescriptor(
            id="cached-read",
            description="test read capability",
            input_schema={
                "type": "object",
                "required": ["path"],
                "properties": {"path": {"type": "string"}},
                "additionalProperties": False,
            },
            effects=frozenset({EffectClass.READ_LOCAL}),
            cache_policy="ttl",
        )
    )
    write_executor = _SlowExecutor(
        CapabilityDescriptor(
            id="cached-write",
            description="test write capability",
            input_schema={
                "type": "object",
                "required": ["operation", "path"],
                "properties": {
                    "operation": {"const": "write"},
                    "path": {"type": "string"},
                },
                "additionalProperties": False,
            },
            effects=frozenset({EffectClass.WRITE_LOCAL}),
        )
    )
    registry = CapabilityRegistry()
    registry.register(read_executor)
    registry.register(write_executor)
    dispatcher = CapabilityDispatcher(registry, PolicyEngine("autonomous"))
    workspace = _workspace(tmp_path)

    first = await dispatcher.dispatch(
        _request("cached-read", "read-1", path="state.json"),
        workspace=workspace,
        profile="supervised",
    )
    second = await dispatcher.dispatch(
        _request("cached-read", "read-2", path="state.json"),
        workspace=workspace,
        profile="supervised",
    )
    await dispatcher.dispatch(
        _request("cached-write", "write-1", operation="write", path="state.json"),
        workspace=workspace,
        profile="autonomous",
    )
    third = await dispatcher.dispatch(
        _request("cached-read", "read-3", path="state.json"),
        workspace=workspace,
        profile="supervised",
    )

    assert first.status is CapabilityResultStatus.OK
    assert second.status is CapabilityResultStatus.OK
    assert second.metadata["cache_hit"] is True
    assert third.status is CapabilityResultStatus.OK
    assert "cache_hit" not in third.metadata
    assert len(read_executor.invocations) == 2


async def test_filesystem_read_cache_is_target_content_addressed(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("one")
    registry = CapabilityRegistry()
    registry.register(FilesystemCapability())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine("autonomous"))
    workspace = _workspace(tmp_path)

    first = await dispatcher.dispatch(
        _request("fs", "fs-read-1", operation="read", path="state.json"),
        workspace=workspace,
        profile="autonomous",
    )
    second = await dispatcher.dispatch(
        _request("fs", "fs-read-2", operation="read", path="state.json"),
        workspace=workspace,
        profile="autonomous",
    )
    target.write_text("two")
    third = await dispatcher.dispatch(
        _request("fs", "fs-read-3", operation="read", path="state.json"),
        workspace=workspace,
        profile="autonomous",
    )

    assert first.output == "one"
    assert second.output == "one"
    assert second.metadata["cache_hit"] is True
    assert third.output == "two"
    assert "cache_hit" not in third.metadata
