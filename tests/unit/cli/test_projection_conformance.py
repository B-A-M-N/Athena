from io import StringIO

import pytest

from athena.acp.adapter import EV_TASK_MESSAGE, _event_from_athena_event
from athena.api.sse import encode_frame
from athena.cli.dual_pane import DualPaneSurface
from athena.cli.oi_stream import OIStreamViewer
from athena.cli.projection import ProjectionState
from athena.cli.scene import build_oi_scene
from athena.protocol.events import make_event


def _snapshot(state: ProjectionState) -> dict:
    return {
        "status": state.status,
        "message": state.status_message,
        "thinking": state.thinking,
        "active": state.active_operation_id,
        "operations": {
            key: (value.label, value.state, value.execution_id, list(value.output))
            for key, value in state.operations.items()
        },
        "stream": list(state.stream),
        "recent": list(state.recent),
    }


@pytest.mark.asyncio
async def test_same_trace_produces_equivalent_semantics_in_cli_surfaces():
    events = [
        make_event("TaskStarted", {}),
        make_event(
            "CapabilityRequested",
            {"call_id": "call-1", "capability_id": "execute", "arguments": {"code": "pytest -q"}},
        ),
        make_event(
            "ExecutionStarted",
            {"call_id": "call-1", "execution_id": "exec-1", "runtime": "python"},
        ),
        make_event(
            "StdoutChunk",
            {"execution_id": "exec-1", "data": "passed\n"},
        ),
        make_event(
            "CapabilityProgress",
            {"call_id": "call-1", "message": "checking"},
        ),
        make_event("CapabilityCompleted", {"call_id": "call-1"}),
        make_event("TaskCompleted", {}),
    ]
    expected = ProjectionState()
    dual = DualPaneSurface(output=StringIO(), error=StringIO(), interactive=False)
    stream = OIStreamViewer(output=StringIO(), interactive=False)

    for event in events:
        expected.reduce(event.type, event.payload)
        dual._ingest_event(event.type, dict(event.payload))
        await stream.handle_event(event)

    assert _snapshot(dual.projection) == _snapshot(expected)
    assert _snapshot(stream.projection) == _snapshot(expected)


@pytest.mark.asyncio
async def test_rich_event_trace_stays_equivalent_across_cli_frontends():
    """Every CLI projection consumes the same lifecycle facts once.

    The approval request below is the kernel's count-only summary form, so
    this test exercises the paused state without asking an interactive viewer
    to make a new operator decision while replaying the trace.
    """
    events = [
        make_event("TaskStarted", {}),
        make_event(
            "CapabilityRequested",
            {"call_id": "call-2", "capability_id": "fs",
             "arguments": {"operation": "read", "path": "src/app.py"}},
        ),
        make_event("CapabilityStarted", {"call_id": "call-2", "capability_id": "fs"}),
        make_event("ExecutionStarted", {"call_id": "call-2", "execution_id": "exec-2", "runtime": "python"}),
        make_event("StdoutChunk", {"execution_id": "exec-2", "data": "checked\n"}),
        make_event("CapabilityProgress", {"call_id": "call-2", "message": "checking"}),
        make_event("CapabilityCompleted", {"call_id": "call-2", "capability_id": "fs", "output": "ok"}),
        make_event("ChildTaskCreated", {"child_task_id": "child-1", "parent_task_id": "task-1"}),
        make_event("GeneratedCapabilityCreated", {"capability": "generated.parser", "status": "candidate"}),
        make_event("ResearchStarted", {"query": "runtime contract"}),
        make_event("ArtifactCreated", {"call_id": "call-2", "uri": "artifact://report"}),
        make_event("VerificationCompleted", {"criterion": "report exists", "status": "passed"}),
        make_event("ApprovalRequested", {"call_id": "call-approval", "capability_id": "execute"}),
        make_event("ApprovalResolved", {"call_id": "call-approval", "decision": "denied"}),
        make_event("RuntimeStateLost", {"runtime_session_id": "runtime-1"}),
        make_event("TaskCancelled", {}),
    ]
    expected = ProjectionState()
    dual = DualPaneSurface(output=StringIO(), error=StringIO(), interactive=False)
    stream = OIStreamViewer(output=StringIO(), error=StringIO(), interactive=False)

    for event in events:
        expected.reduce(event.type, event.payload)
        dual._ingest_event(event.type, dict(event.payload))
        await stream.handle_event(event)

    assert _snapshot(dual.projection) == _snapshot(expected)
    assert _snapshot(stream.projection) == _snapshot(expected)
    assert dual.projection.operations["call-2"].output[-1] == "ok"
    assert any(entity.id == "generated_tool:generated.parser"
               for entity in build_oi_scene(dual.projection, dual.layout.oi).entities)


def test_transport_projections_preserve_canonical_event_truth():
    event = make_event(
        "CapabilityCompleted",
        {"call_id": "call-1", "capability_id": "execute", "output": "ok"},
        task_id="task-1",
    )
    projection = ProjectionState()
    projection.reduce(event.type, event.payload)

    sse = encode_frame(event)
    acp = _event_from_athena_event(event, "task-1")

    assert projection.operations["call-1"].state == "complete"
    assert "event: CapabilityCompleted" in sse
    assert '"output": "ok"' in sse
    assert acp.type == EV_TASK_MESSAGE
    assert acp.task_id == "task-1"
    assert acp.payload["capability_id"] == "execute"
    assert acp.payload["output"] == "ok"


def test_restart_loss_is_a_warning_in_the_shared_projection():
    state = ProjectionState()
    state.reduce(
        "RuntimeStateLost",
        {"runtime_session_id": "runtime-1"},
    )

    assert state.status == "WARNING"
    assert "runtime-1" in state.recent[-1][1]
