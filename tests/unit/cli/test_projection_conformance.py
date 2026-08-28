from io import StringIO

import pytest

from athena.acp.adapter import EV_TASK_MESSAGE, _event_from_athena_event
from athena.api.sse import encode_frame
from athena.cli.dual_pane import DualPaneSurface
from athena.cli.activity import VisualActionKind
from athena.cli.layout import Rect
from athena.cli.oi_stream import OIStreamViewer
from athena.cli.native_bridge import native_projection_frame
from athena.cli.projection import ProjectionState
from athena.cli.render.scene import render_scene_lines
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
            {
                "call_id": "call-2",
                "capability_id": "fs",
                "arguments": {"operation": "read", "path": "src/app.py"},
            },
        ),
        make_event("CapabilityStarted", {"call_id": "call-2", "capability_id": "fs"}),
        make_event(
            "ExecutionStarted", {"call_id": "call-2", "execution_id": "exec-2", "runtime": "python"}
        ),
        make_event("StdoutChunk", {"execution_id": "exec-2", "data": "checked\n"}),
        make_event("CapabilityProgress", {"call_id": "call-2", "message": "checking"}),
        make_event(
            "CapabilityCompleted", {"call_id": "call-2", "capability_id": "fs", "output": "ok"}
        ),
        make_event("ChildTaskCreated", {"child_task_id": "child-1", "parent_task_id": "task-1"}),
        make_event(
            "GeneratedCapabilityCreated", {"capability": "generated.parser", "status": "candidate"}
        ),
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
    assert any(
        entity.id == "generated_tool:generated.parser"
        for entity in build_oi_scene(dual.projection, dual.layout.oi).entities
    )


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


def test_projection_renders_canonical_determinate_progress():
    projection = ProjectionState()
    projection.reduce(
        "CapabilityRequested",
        {"call_id": "call-progress", "capability_id": "workflow"},
    )
    projection.reduce(
        "CapabilityProgress",
        {
            "call_id": "call-progress",
            "capability_id": "workflow",
            "value": 2,
            "total": 5,
            "unit": "steps",
            "determinate": True,
            "message": "workflow step two complete (2/5)",
        },
    )

    operation = projection.operations["call-progress"]
    assert operation.progress == "2/5"
    assert operation.progress_value == 0.4
    assert operation.progress_determinate is True


def test_restart_loss_is_a_warning_in_the_shared_projection():
    state = ProjectionState()
    state.reduce(
        "RuntimeStateLost",
        {"runtime_session_id": "runtime-1"},
    )

    assert state.status == "WARNING"
    assert "runtime-1" in state.recent[-1][1]


def test_native_bridge_uses_the_same_scene_projection_as_hosted_surfaces():
    state = ProjectionState()
    state.reduce(
        "CapabilityRequested",
        {
            "call_id": "call-native",
            "capability_id": "execute",
            "arguments": {"command": "pytest -q"},
        },
    )
    state.reduce(
        "CapabilityStarted",
        {"call_id": "call-native", "capability_id": "execute"},
    )

    frame = native_projection_frame(state, width=60, height=16)

    assert frame["title"] == "ATHENA OI // GLASS COMPUTE"
    assert frame["status"] == "EXECUTING"
    assert len(frame["oi"]) == 16
    entity = next(item for item in frame["entities"] if item["id"] == "call-native")
    assert entity["kind"] == "operation"
    assert entity["label"] == "execute"
    assert frame["schema_version"] == 2
    assert frame["semantic_state"] == "test"
    assert frame["buddy"]["character"] == "owl"
    assert any(item["id"] == "call-native" for item in frame["runtime_entities"])


def test_code_mutation_content_is_projected_to_ansi_and_native_surfaces():
    state = ProjectionState()
    state.reduce(
        "CapabilityRequested",
        {
            "call_id": "call-write",
            "capability_id": "fs",
            "arguments": {
                "operation": "write",
                "path": "src/repair.py",
                "content": "def repair(value):\n    return value.strip()\n",
            },
        },
    )
    state.reduce("CapabilityStarted", {"call_id": "call-write"})
    state.reduce(
        "MutationPrepared",
        {
            "call_id": "call-write",
            "resource": "src/repair.py",
            "operation": "write",
        },
    )
    state.reduce("CapabilityCompleted", {"call_id": "call-write"})

    scene = build_oi_scene(state, Rect(0, 0, 80, 24))
    assert scene.mode is VisualActionKind.CODE
    assert scene.code_view is not None
    assert "return value.strip()" in scene.code_view.text
    assert scene.code_view.mutation_state == "applied"
    ansi = "\n".join(render_scene_lines(state, scene, width=80, height=24, buddy_enabled=False))
    assert "CODE // src/repair.py" in ansi
    assert "return value.strip()" in ansi

    frame = native_projection_frame(state, width=80, height=24)
    assert frame["semantic_state"] == "code"
    assert frame["code_view"]["path"] == "src/repair.py"
    assert "return value.strip()" in frame["code_view"]["text"]
    assert frame["active_operation"]["mutation_state"] == "applied"


def test_diagnostics_and_verification_are_first_class_projection_facts():
    state = ProjectionState()
    state.reduce(
        "CapabilityRequested",
        {
            "call_id": "call-check",
            "capability_id": "execute",
            "arguments": {"language": "python", "code": "pytest -q"},
        },
    )
    state.reduce(
        "DiagnosticsProduced",
        {
            "call_id": "call-check",
            "diagnostics": [{"path": "src/a.py", "line": 4, "message": "bad value"}],
        },
    )
    state.reduce("VerificationStarted", {"call_id": "call-check"})
    state.reduce(
        "VerificationCheckCompleted",
        {
            "call_id": "call-check",
            "criterion": "tests",
            "status": "failed",
        },
    )
    state.reduce("VerificationCompleted", {"call_id": "call-check", "status": "failed"})

    scene = build_oi_scene(state, Rect(0, 0, 80, 24))
    assert scene.mode is VisualActionKind.FAILURE
    assert scene.diagnostics[0]["message"] == "bad value"
    assert scene.verification_checks[0]["criterion"] == "tests"
    assert state.verification_status == "failed"
    frame = native_projection_frame(state, width=80, height=24)
    assert frame["diagnostics"][0]["path"] == "src/a.py"
    assert frame["verification"]["status"] == "failed"
