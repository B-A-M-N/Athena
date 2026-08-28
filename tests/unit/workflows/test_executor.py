from __future__ import annotations

import json

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
)
from athena.protocol.tasks import CapabilityPolicy, WorkspaceSpec
from athena.workflows import Workflow, WorkflowExecutor, WorkflowStep


class _Dispatcher:
    def __init__(self) -> None:
        self.requests: list[CapabilityRequest] = []
        self.dispatch_kwargs: list[dict] = []

    async def dispatch(self, request, **kwargs):
        self.requests.append(request)
        self.dispatch_kwargs.append(kwargs)
        return CapabilityResult(
            request.call_id, request.capability_id,
            CapabilityResultStatus.OK,
            output=json.dumps(request.arguments),
        )


def _resolver(identifier):
    if identifier == "echo":
        return CapabilityDescriptor(
            id="echo", description="echo", input_schema={"type": "object"})
    raise KeyError(identifier)


async def test_workflow_supports_conditions_and_bounded_foreach(tmp_path):
    dispatcher = _Dispatcher()
    workflow = Workflow.create(
        name="batch",
        description="batch echo",
        steps=(
            WorkflowStep(
                id="echoes",
                capability_id="echo",
                arguments={"value": "$item"},
                if_condition="$run == true",
                foreach="$items",
            ),
        ),
    )
    result = await WorkflowExecutor(
        dispatcher, resolver=_resolver,
    ).run(
        workflow,
        task_id="task-1",
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        inputs={"run": True, "items": ["a", "b"]},
    )

    assert result.status == "completed"
    assert [call.arguments["value"] for call in dispatcher.requests] == [
        "a", "b",
    ]
    assert result.outputs["echoes"] == [
        '{"value": "a"}', '{"value": "b"}',
    ]
    assert {call.session_id for call in dispatcher.requests} == {None}


async def test_workflow_propagates_session_scope_to_capability_calls(tmp_path):
    dispatcher = _Dispatcher()
    workflow = Workflow.create(
        name="session-aware", description="session-aware",
        steps=(WorkflowStep(id="echo", capability_id="echo"),),
    )

    result = await WorkflowExecutor(dispatcher, resolver=_resolver).run(
        workflow,
        task_id="task-1",
        session_id="session-1",
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
    )

    assert result.status == "completed"
    assert dispatcher.requests[0].session_id == "session-1"


async def test_workflow_propagates_task_capability_policy_to_steps(tmp_path):
    dispatcher = _Dispatcher()
    workflow = Workflow.create(
        name="policy-aware", description="policy-aware",
        steps=(WorkflowStep(id="echo", capability_id="echo"),),
    )
    policy = CapabilityPolicy(allow=("echo",))

    result = await WorkflowExecutor(dispatcher, resolver=_resolver).run(
        workflow,
        task_id="task-1",
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        task_policy=policy,
    )

    assert result.status == "completed"
    assert dispatcher.dispatch_kwargs[0]["task_policy"] == policy


async def test_workflow_rejects_unbounded_foreach(tmp_path):
    dispatcher = _Dispatcher()
    workflow = Workflow.create(
        name="bounded",
        description="bounded",
        steps=(
            WorkflowStep(
                id="echoes", capability_id="echo", arguments={"value": "$item"},
                foreach="$items", max_iterations=1,
            ),
        ),
    )
    result = await WorkflowExecutor(
        dispatcher, resolver=_resolver,
    ).run(
        workflow,
        task_id="task-1",
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        inputs={"items": ["a", "b"]},
    )
    assert result.status == "failed"
    assert "max_iterations" in result.failures[0]
    assert dispatcher.requests == []


async def test_workflow_validates_dynamic_input_contract_before_steps(tmp_path):
    dispatcher = _Dispatcher()
    workflow = Workflow.create(
        name="requires-target",
        description="requires a target",
        input_schema={
            "type": "object",
            "required": ["target"],
            "properties": {"target": {"type": "string"}},
            "additionalProperties": False,
        },
        steps=(WorkflowStep(id="echo", capability_id="echo", arguments={"value": "$target"}),),
    )

    result = await WorkflowExecutor(
        dispatcher, resolver=_resolver,
    ).run(
        workflow,
        task_id="task-1",
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        inputs={},
    )

    assert result.status == "invalid"
    assert "target" in result.failures[0]
    assert dispatcher.requests == []
