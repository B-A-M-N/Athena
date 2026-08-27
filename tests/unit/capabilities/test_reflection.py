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
from athena.protocol.tasks import WorkspaceSpec
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
