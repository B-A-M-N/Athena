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
        return [
            Workflow(
                id="workflow.verify",
                name="verify project",
                description="Inspect project and run verification",
                steps=(WorkflowStep(id="inspect", capability_id="project.inspect"),),
            )
        ]

    async def get(self, workflow_id, **kwargs):
        return (await self.list())[0] if workflow_id == "workflow.verify" else None


class _Skills:
    async def search(self, query="", *, limit=10):
        return [
            Skill(
                id="skill.project-debug",
                name="project debugging",
                description="Debug project verification failures",
                body="Inspect logs and reproduce the failure.",
                scope="project",
            )
        ][:limit]

    async def load_active(self):
        return await self.search()


class _Approvals:
    async def list_pending(self, task_id):
        return [
            {
                "id": "approval-1",
                "capability_id": "execute",
                "status": "PENDING",
                "created_at": "2026-01-01T00:00:00Z",
                "arguments": {"secret": "must-not-leak"},
            }
        ]


@pytest.mark.asyncio
async def test_reflection_searches_capabilities_workflows_and_skills():
    registry = CapabilityRegistry()
    registry.register(_Capability())
    fabric = CapabilityFabric(registry)
    reflection = CapabilityReflection(
        fabric,
        workflow_store=_Workflows(),
        skills_store=_Skills(),
    )
    result = await reflection.invoke(
        CapabilityRequest(
            capability_id="capabilities",
            task_id="task-1",
            call_id="call-1",
            arguments={"operation": "search", "query": "project"},
        ),
        context=SimpleNamespace(workspace=WorkspaceSpec(id="repo", root="/tmp")),
    )

    assert result.status is CapabilityResultStatus.OK
    kinds = {item["kind"] for item in json.loads(result.output)}
    assert kinds == {"capability", "workflow", "skill"}
    results = json.loads(result.output)
    assert all("score" in item for item in results)
    assert results == sorted(results, key=lambda item: (-item["score"], item["kind"], item["id"]))


@pytest.mark.asyncio
async def test_reflection_describes_workflow_without_exposing_skill_body():
    reflection = CapabilityReflection(
        CapabilityFabric(CapabilityRegistry()),
        workflow_store=_Workflows(),
        skills_store=_Skills(),
    )
    result = await reflection.invoke(
        CapabilityRequest(
            capability_id="capabilities",
            task_id="task-1",
            call_id="call-2",
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
            capability_id="capabilities",
            task_id="task-1",
            call_id="call-3",
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
    assert permissions[0]["pending_approvals"] == [
        {
            "approval_id": "approval-1",
            "capability_id": "execute",
            "status": "PENDING",
            "created_at": "2026-01-01T00:00:00Z",
        }
    ]
    assert permissions[1]["task_allowed"] is True


@pytest.mark.asyncio
async def test_reflection_availability_explains_task_blockers():
    registry = CapabilityRegistry()
    registry.register(_Capability())
    reflection = CapabilityReflection(CapabilityFabric(registry))
    result = await reflection.invoke(
        CapabilityRequest(
            capability_id="capabilities",
            task_id="task-availability",
            call_id="call-availability",
            arguments={
                "operation": "availability",
                "capability_id": "project.inspect",
            },
        ),
        context=SimpleNamespace(
            workspace=WorkspaceSpec(id="repo", root="/tmp"),
            capability_policy=CapabilityPolicy(deny=("project.inspect",)),
        ),
    )

    assert result.status is CapabilityResultStatus.OK
    passport = json.loads(result.output)
    assert passport["kind"] == "environment_passport"
    assert passport["status"] == "BLOCKED"
    assert any(
        check["kind"] == "policy" and check["status"] == "denied" for check in passport["checks"]
    )


@pytest.mark.asyncio
async def test_reflection_availability_reports_ready_capability():
    registry = CapabilityRegistry()
    registry.register(_Capability())
    reflection = CapabilityReflection(CapabilityFabric(registry))
    result = await reflection.invoke(
        CapabilityRequest(
            capability_id="capabilities",
            task_id="task-ready",
            call_id="call-ready",
            arguments={
                "operation": "availability",
                "capability_id": "project.inspect",
            },
        ),
        context=SimpleNamespace(
            workspace=WorkspaceSpec(id="repo", root="/tmp"),
        ),
    )

    assert result.status is CapabilityResultStatus.OK
    assert json.loads(result.output)["status"] == "AVAILABLE"


@pytest.mark.asyncio
async def test_reflection_availability_reports_missing_generated_prerequisite(tmp_path):
    from athena.affordances.models import AffordanceScope, GeneratedCapability

    registry = CapabilityRegistry()
    registry.register(_Capability())
    fabric = CapabilityFabric(registry)
    generated = GeneratedCapability(
        id="generated.needs-missing",
        name="needs missing",
        description="generated capability with a native prerequisite",
        implementation="def run(args):\n    return {'ok': True}\n",
        input_schema={"type": "object"},
        required_capabilities=("missing.native",),
        scope=AffordanceScope.PROJECT,
        project_scope="repo",
        validation_state="PROMOTED",
        proof_record={"all_passed": True},
        lifecycle_state="PROMOTED",
    )

    class _Generated:
        descriptor = CapabilityDescriptor(
            id=generated.id,
            description=generated.description,
            input_schema=generated.input_schema,
            effects=frozenset({EffectClass.READ_LOCAL}),
            origin=CapabilityOrigin.PROJECT,
        )

    fabric.register_project("repo", _Generated(), generated=generated)
    reflection = CapabilityReflection(fabric)
    result = await reflection.invoke(
        CapabilityRequest(
            capability_id="capabilities",
            task_id="task-prereq",
            call_id="call-prereq",
            arguments={
                "operation": "availability",
                "capability_id": generated.id,
            },
        ),
        context=SimpleNamespace(
            workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        ),
    )

    passport = json.loads(result.output)
    assert passport["status"] == "BLOCKED"
    assert any(
        check["kind"] == "prerequisites" and check["status"] == "missing"
        for check in passport["checks"]
    )


@pytest.mark.asyncio
async def test_reflection_availability_reports_open_capability_circuit():
    from athena.capabilities.health import CapabilityHealth

    registry = CapabilityRegistry()
    registry.register(_Capability())
    health = CapabilityHealth(failure_threshold=1, cooldown_seconds=60)
    health.record_failure("project.inspect", "executor unavailable")
    reflection = CapabilityReflection(
        CapabilityFabric(registry),
        health_provider=health,
    )

    result = await reflection.invoke(
        CapabilityRequest(
            capability_id="capabilities",
            task_id="task-health",
            call_id="call-health",
            arguments={
                "operation": "availability",
                "capability_id": "project.inspect",
            },
        ),
        context=SimpleNamespace(
            workspace=WorkspaceSpec(id="repo", root="/tmp"),
        ),
    )

    assert result.status is CapabilityResultStatus.OK
    passport = json.loads(result.output)
    assert passport["status"] == "BLOCKED"
    assert any(
        check["kind"] == "health" and check["status"] == "open" for check in passport["checks"]
    )
    assert "wait for its cooldown" in passport["preconditions"][0]


@pytest.mark.asyncio
async def test_reflection_availability_reports_missing_execution_backend(tmp_path):
    class _Execution:
        def backend_status(self):
            return [{"id": "container", "available": False, "healthy": False}]

    registry = CapabilityRegistry()
    registry.register(_Capability())
    reflection = CapabilityReflection(
        CapabilityFabric(registry),
        execution_manager=_Execution(),
    )
    workspace = WorkspaceSpec(
        id="repo",
        root=str(tmp_path),
        execution_backend="container",
    )

    result = await reflection.invoke(
        CapabilityRequest(
            capability_id="capabilities",
            task_id="task-backend",
            call_id="call-backend",
            arguments={
                "operation": "availability",
                "capability_id": "project.inspect",
            },
        ),
        context=SimpleNamespace(workspace=workspace),
    )

    passport = json.loads(result.output)
    assert passport["status"] == "BLOCKED"
    assert any(
        check["kind"] == "execution_backend" and check["status"] == "missing"
        for check in passport["checks"]
    )


def test_reflection_makes_unsupported_devices_explicit():
    reflection = CapabilityReflection(CapabilityFabric(CapabilityRegistry()))

    assert reflection._list_devices() == [
        {
            "kind": "device_provider",
            "status": "unsupported",
            "reason": "no device provider is configured",
        }
    ]


def test_execution_manager_reports_runtime_health_and_aliases():
    from athena.execution.manager import ExecutionManager

    class _Runtime:
        name = "python"
        aliases = ("py",)
        persistence = "persistent"

    manager = ExecutionManager()
    runtime = _Runtime()
    manager.register_runtime(runtime)

    assert manager.runtime_status() == [
        {
            "id": "python",
            "aliases": ["py"],
            "available": True,
            "healthy": True,
            "persistence": "persistent",
            "active_sessions": 0,
            "active_executions": 0,
            "implementation": "_Runtime",
        }
    ]
