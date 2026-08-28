from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from athena.affordances import CapabilityFabric
from athena.capabilities.observer import ObserverCapability
from athena.capabilities.registry import CapabilityRegistry
from athena.capabilities.synthesis import SynthesisCapability
from athena.protocol.capabilities import CapabilityRequest, CapabilityResultStatus
from athena.protocol.tasks import WorkspaceSpec
from athena.synthesis.engine import SynthesisEngine


@pytest.mark.asyncio
async def test_observer_compiles_and_reuses_structured_sensor(tmp_path):
    fabric = CapabilityFabric(CapabilityRegistry())
    engine = SynthesisEngine()
    synthesis = SynthesisCapability(engine, fabric)

    class _Dispatcher:
        def __init__(self):
            self.calls = []

        async def dispatch(self, request, *, workspace, **kwargs):
            self.calls.append((request, workspace, kwargs))
            executor = fabric.executor_for(
                request.capability_id,
                task_id=request.task_id,
                project_id=workspace.id,
                user_id="athena",
            )
            return await executor.invoke(
                request,
                context=SimpleNamespace(workspace=workspace),
            )

    dispatcher = _Dispatcher()
    observer = ObserverCapability(synthesis, fabric, dispatcher=dispatcher)

    created = await observer.invoke(
        CapabilityRequest(
            capability_id="observer",
            task_id="task-observe",
            call_id="create-observe",
            arguments={
                "operation": "create",
                "name": "parse_diagnostics",
                "description": "Normalize compiler diagnostics",
                "code": (
                    "def run(args):\n"
                    "    lines = args['input'].splitlines()\n"
                    "    return {'errors': [line for line in lines if 'error:' in line]}\n"
                ),
                "validation_cases": [
                    {
                        "args": {"input": "ok\nerror: missing name\n"},
                        "expect_output": {"errors": ["error: missing name"]},
                    }
                ],
            },
        )
    )
    assert created.status is CapabilityResultStatus.OK, created.error
    observer_id = json.loads(created.output)["capability_id"]

    result = await observer.invoke(
        CapabilityRequest(
            capability_id="observer",
            task_id="task-observe",
            call_id="run-observe",
            arguments={
                "operation": "run",
                "observer_id": observer_id,
                "input": "error: bad type\nwarning: ignored\n",
            },
        ),
        context=SimpleNamespace(
            workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        ),
    )
    assert result.status is CapabilityResultStatus.OK, result.error
    assert json.loads(result.output) == {"errors": ["error: bad type"]}
    assert len(dispatcher.calls) == 1
    assert dispatcher.calls[0][0].origin.value == "generated"
    assert dispatcher.calls[0][0].call_id != "run-observe"


@pytest.mark.asyncio
async def test_watch_publishes_generated_observation(tmp_path):
    from athena.capabilities.watch import WatchRegistry, _FileWatch

    workspace = WorkspaceSpec(id="repo", root=str(tmp_path))
    runner_calls = []

    async def runner(task_id, observer_id, value, observed_workspace, **kwargs):
        runner_calls.append((task_id, observer_id, value, observed_workspace))
        return {"status": "ok", "value": {"changed": value["changes"]}}

    path = tmp_path / "state.txt"
    path.write_text("before", encoding="utf-8")
    registry = WatchRegistry(observer_runner=runner)
    registry.file_watches["watch-1"] = _FileWatch(
        "watch-1",
        str(tmp_path),
        "*.txt",
        "task-observe",
        workspace=workspace,
        observer_id="synth_sensor",
    )
    path.write_text("after", encoding="utf-8")
    events = []

    async def sink(event_type, payload, *, task_id):
        events.append((event_type, payload, task_id))

    assert await registry.poll_all(sink) == 1
    assert runner_calls[0][0:2] == ("task-observe", "synth_sensor")
    assert events[0][1]["observation"]["value"]["changed"] == ["state.txt"]
