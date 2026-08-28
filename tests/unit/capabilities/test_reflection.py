from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from athena.affordances import CapabilityFabric
from athena.capabilities.reflection import CapabilityReflection
from athena.capabilities.registry import CapabilityRegistry
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.tasks import AutonomyLevel, CapabilityPolicy, WorkspaceSpec
from athena.skills.models import Skill
from athena.workflows.models import Workflow, WorkflowStep


class _Capability:
    descriptor = CapabilityDescriptor(
        id="project.inspect",
        description="Inspect project source and test output",
        input_schema={"type": "object"},
        effects=frozenset({EffectClass.READ_LOCAL}),
        origin=CapabilityOrigin.PROJECT,
    )


class _Workflows:
    async def list(self, **kwargs):
        return [Workflow(
            id="workflow.verify",
            name="verify project",
            description="Inspect project and run verification",
            steps=(WorkflowStep(id="inspect", capability_id="project.inspect"),),
        )]

    async def get(self, workflow_id, **kwargs):
        return (await self.list())[0] if workflow_id == "workflow.verify" else None


class _Skills:
    async def search(self, query="", *, limit=10):
        return [Skill(
            id="skill.project-debug",
            name="project debugging",
            description="Debug project verification failures",
            body="Inspect logs and reproduce the failure.",
            scope="project",
        )][:limit]

    async def load_active(self):
        return await self.search()


class _Approvals:
    async def list_pending(self, task_id):
        return [{
            "id": "approval-1", "capability_id": "execute",
            "status": "PENDING", "created_at": "2026-01-01T00:00:00Z",
            "arguments": {"secret": "must-not-leak"},
        }]


@pytest.mark.asyncio
async def test_reflection_searches_capabilities_workflows_and_skills():
    registry = CapabilityRegistry()
    registry.register(_Capability())
    fabric = CapabilityFabric(registry)
    reflection = CapabilityReflection(
        fabric, workflow_store=_Workflows(), skills_store=_Skills(),
    )
    result = await reflection.invoke(
        CapabilityRequest(
            capability_id="capabilities", task_id="task-1", call_id="call-1",
            arguments={"operation": "search", "query": "project"},
        ),
        context=SimpleNamespace(workspace=WorkspaceSpec(id="repo", root="/tmp")),
    )

    assert result.status is CapabilityResultStatus.OK
    kinds = {item["kind"] for item in json.loads(result.output)}
    assert kinds == {"capability", "workflow", "skill"}


@pytest.mark.asyncio
async def test_reflection_describes_workflow_without_exposing_skill_body():
    reflection = CapabilityReflection(
        CapabilityFabric(CapabilityRegistry()), workflow_store=_Workflows(),
        skills_store=_Skills(),
    )
    result = await reflection.invoke(
        CapabilityRequest(
            capability_id="capabilities", task_id="task-1", call_id="call-2",
            arguments={"operation": "describe", "workflow_id": "workflow.verify"},
        ),
    )

    assert result.status is CapabilityResultStatus.OK
    described = json.loads(result.output)
    assert described["kind"] == "workflow"
    assert described["steps"][0]["capability"] == "project.inspect"


@pytest.mark.asyncio
async def test_reflection_permissions_reports_effective_task_authority():
    registry = CapabilityRegistry()
    registry.register(_Capability())
    policy = SimpleNamespace(
        profile=AutonomyLevel.CODING,
        approvals=SimpleNamespace(list_active=lambda: []),
    )
    reflection = CapabilityReflection(
        CapabilityFabric(registry),
        policy_engine=policy,
        approval_store=_Approvals(),
    )
    result = await reflection.invoke(
        CapabilityRequest(
            capability_id="capabilities", task_id="task-1", call_id="call-3",
            arguments={"operation": "permissions"},
        ),
        context=SimpleNamespace(
            workspace=WorkspaceSpec(id="repo", root="/tmp"),
            capability_policy=CapabilityPolicy(allow=("project.inspect",)),
        ),
    )

    assert result.status is CapabilityResultStatus.OK
    permissions = json.loads(result.output)
    assert permissions[0]["kind"] == "policy_context"
    assert permissions[0]["profile"] == "coding"
    assert permissions[0]["pending_approvals"] == [{
        "approval_id": "approval-1", "capability_id": "execute",
        "status": "PENDING", "created_at": "2026-01-01T00:00:00Z",
    }]
    assert permissions[1]["task_allowed"] is True


def test_reflection_makes_unsupported_devices_explicit():
    reflection = CapabilityReflection(CapabilityFabric(CapabilityRegistry()))

    assert reflection._list_devices() == [{
        "kind": "device_provider",
        "status": "unsupported",
        "reason": "no device provider is configured",
    }]


def test_execution_manager_reports_runtime_health_and_aliases():
    from athena.execution.manager import ExecutionManager

    class _Runtime:
        name = "python"
        aliases = ("py",)
        persistence = "persistent"

    manager = ExecutionManager()
    runtime = _Runtime()
    manager.register_runtime(runtime)

    assert manager.runtime_status() == [{
        "id": "python",
        "aliases": ["py"],
        "available": True,
        "healthy": True,
        "persistence": "persistent",
        "active_sessions": 0,
        "active_executions": 0,
        "implementation": "_Runtime",
    }]
