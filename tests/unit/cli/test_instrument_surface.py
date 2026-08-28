import asyncio

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.registry import CapabilityRegistry
from athena.cli.native_bridge import native_projection_frame
from athena.cli.projection import ProjectionState
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.tasks import WorkspaceSpec
from athena.policy.engine import PolicyEngine


class _InstrumentExecutor:
    descriptor = CapabilityDescriptor(
        id="graph_builder",
        description="graph",
        input_schema={"type": "object"},
        effects=frozenset({EffectClass.READ_LOCAL}),
    )

    async def invoke(self, request, *, output_accumulator=None, context=None):
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.OK,
            output="done",
            metadata={
                "instrument": {
                    "id": "graph-1",
                    "kind": "graph",
                    "title": "Dependency graph",
                    "payload": {"nodes": ["a", "b"], "edges": [["a", "b"]]},
                }
            },
        )


def test_instrument_view_is_emitted_by_dispatcher_and_projected():
    events = []
    registry = CapabilityRegistry()
    registry.register(_InstrumentExecutor())

    async def sink(event):
        events.append(event)

    dispatcher = CapabilityDispatcher(
        registry,
        PolicyEngine("offline"),
        event_sink=sink,
    )
    result = asyncio.run(
        dispatcher.dispatch(
            CapabilityRequest(
                capability_id="graph_builder",
                arguments={},
                task_id="task-1",
                call_id="call-1",
            ),
            workspace=WorkspaceSpec(id="repo", root="/tmp/repo"),
        )
    )
    assert result.status is CapabilityResultStatus.OK
    event = next(item for item in events if item.type == "InstrumentProduced")
    state = ProjectionState()
    state.reduce(event.type, event.payload)
    assert state.instruments[0]["kind"] == "graph"
    frame = native_projection_frame(state, width=60, height=20)
    assert frame["instruments"][0]["title"] == "Dependency graph"
