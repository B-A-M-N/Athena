from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from athena.affordances import CapabilityFabric
from athena.capabilities.capsule import ProcedureCapsuleCapability
from athena.capabilities.registry import CapabilityRegistry
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
)
from athena.protocol.tasks import WorkspaceSpec
from athena.synthesis.engine import SynthesisEngine
from athena.workflows.models import Workflow, WorkflowStep


class _Workflows:
    def __init__(self):
        self.items = {}

    async def save(self, workflow):
        self.items[workflow.id] = workflow

    async def get(self, workflow_id, **kwargs):
        workflow = self.items.get(workflow_id)
        if workflow is None:
            return None
        task_id = kwargs.get("task_id")
        if workflow.scope.value == "task" and workflow.task_scope != task_id:
            return None
        return workflow

    async def delete(self, workflow_id):
        self.items.pop(workflow_id, None)


@pytest.mark.asyncio
async def test_capsule_exports_and_reimports_generated_procedure(tmp_path):
    source_fabric = CapabilityFabric(CapabilityRegistry())
    source_engine = SynthesisEngine()
    generated = source_engine.synthesize(
        name="capsule_helper",
        description="helper carried by a procedure capsule",
        code="def run(args):\n    return {'value': args['value']}\n",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        task_id="task-source",
    )
    generated = await source_engine.validate(
        generated,
        [{"args": {"value": "ok"}, "expect_output": {"value": "ok"}}],
    )
    assert source_engine.register_ephemeral(source_fabric, generated)
    workflows = _Workflows()
    workflow = Workflow.create(
        name="portable procedure",
        description="replay a generated helper",
        steps=(
            WorkflowStep(
                id="step-1",
                capability_id=generated.id,
                arguments={"value": "ok"},
            ),
        ),
        task_scope="task-source",
    )
    await workflows.save(workflow)

    source_capsule = ProcedureCapsuleCapability(
        workflows,
        source_fabric,
        source_engine,
    )
    exported = await source_capsule.invoke(
        CapabilityRequest(
            capability_id="capsule",
            task_id="task-source",
            call_id="export",
            arguments={"operation": "export", "workflow_id": workflow.id},
        ),
        context=SimpleNamespace(
            workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        ),
    )
    assert exported.status is CapabilityResultStatus.OK, exported.error
    capsule = json.loads(exported.output)
    assert capsule["capsule_id"].startswith("capsule_")
    assert capsule["capabilities"][0]["generated"]["proof_record"]["all_passed"] is True

    target_fabric = CapabilityFabric(CapabilityRegistry())
    target_engine = SynthesisEngine()
    target_capsule = ProcedureCapsuleCapability(
        workflows,
        target_fabric,
        target_engine,
    )
    imported = await target_capsule.invoke(
        CapabilityRequest(
            capability_id="capsule",
            task_id="task-target",
            call_id="import",
            arguments={"operation": "import", "capsule": capsule},
        ),
        context=SimpleNamespace(
            workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        ),
    )
    assert imported.status is CapabilityResultStatus.OK, imported.error
    assert target_fabric.has(generated.id, task_id="task-target")
    assert await workflows.get(workflow.id, task_id="task-target") is not None

    class _Workflow:
        def __init__(self):
            self.calls = []

        async def invoke(self, request, *, context=None):
            self.calls.append(request)
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.OK,
                output="replayed",
            )

    workflow_executor = _Workflow()

    class _Dispatcher:
        async def dispatch(self, request, **kwargs):
            return await workflow_executor.invoke(
                request,
                context=SimpleNamespace(workspace=kwargs["workspace"]),
            )

    replay_fabric = CapabilityFabric(CapabilityRegistry())
    replay_engine = SynthesisEngine()
    replay_capability = ProcedureCapsuleCapability(
        workflows,
        replay_fabric,
        replay_engine,
        workflow_capability=workflow_executor,
        dispatcher=_Dispatcher(),
    )
    replayed = await replay_capability.invoke(
        CapabilityRequest(
            capability_id="capsule",
            task_id="task-target",
            call_id="replay",
            arguments={"operation": "run", "capsule": capsule},
        ),
        context=SimpleNamespace(
            workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        ),
    )
    assert replayed.status is CapabilityResultStatus.OK
    assert workflow_executor.calls[0].capability_id == "workflow"
    assert workflow_executor.calls[0].call_id != "replay"


@pytest.mark.asyncio
async def test_capsule_rejects_tampered_content():
    capability = ProcedureCapsuleCapability(
        _Workflows(), CapabilityFabric(CapabilityRegistry()), SynthesisEngine()
    )
    result = await capability.invoke(
        CapabilityRequest(
            capability_id="capsule",
            task_id="task-1",
            call_id="inspect",
            arguments={
                "operation": "inspect",
                "capsule": {"format": 1, "capsule_id": "capsule_bad", "workflows": []},
            },
        )
    )
    assert result.status is CapabilityResultStatus.FAILED
    assert "hash" in (result.error or "")
