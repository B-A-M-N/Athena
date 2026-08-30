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


def test_live_trace_overflow_cue_renders_at_right_edge_for_long_lines():
    """Regression: long LIVE TRACE rows must stay inside the cell aperture.

    When the terminal is narrower than the trace content the renderer must
    (a) fit/truncate the trace line to the aperture width, and (b) place a
    "…" overflow cue on the last trace row at the rightmost column. The cue
    must actually appear — a previous bug used ``overwrite=False`` for the
    cue cell, causing it to silently fail because ``fit_cells`` had already
    filled that column.
    """
    from athena.cli.scene import OIScene

    state = ProjectionState()
    # Seed the shared stream with a line far wider than the aperture.
    long_text = "x" * 80
    state.feed_stream(long_text)
    state.seal_stream()

    scene = OIScene(
        viewport=Rect(0, 0, 40, 24),
        status="ok",
        title="TEST",
        mode=VisualActionKind.IDLE,
    )

    lines = render_scene_lines(
        state,
        scene,
        width=40,
        height=24,
        buddy_enabled=False,
    )

    # Find the LIVE TRACE section (it appears near the bottom).
    trace_idx = None
    for i, line in enumerate(lines):
        if "LIVE TRACE" in line:
            trace_idx = i
            break

    assert trace_idx is not None, "LIVE TRACE header not found in output"

    # The row immediately after LIVE TRACE must start with the pipe character.
    trace_row = lines[trace_idx + 1]
    assert trace_row.startswith("\u2502 "), f"trace row should start with '│ ': {trace_row!r}"

    # The overflow cue "…" must be at the very last column of the last
    # trace row (second trace line).  The last two rows of the output are
    # the two trace lines.
    assert len(lines) >= trace_idx + 3
    last_trace = lines[trace_idx + 2]
    assert last_trace.endswith("\u2026"), (
        f"Overflow cue '…' missing at right edge. "
        f"Last trace row: {last_trace!r} (width={len(last_trace)})"
    )

    # The total line width must not exceed the aperture width.
    for line in lines:
        assert len(line) <= 40, f"Line exceeds aperture: {line!r} (len={len(line)})"


def test_projection_retains_bounded_runtime_entities_and_partial_output():
    state = ProjectionState()
    for index in range(140):
        task_id = f"task-{index}"
        state.reduce("TaskCreated", {"task_id": task_id, "objective": task_id})
        state.reduce("TaskCompleted", {"task_id": task_id})
    for index in range(300):
        execution_id = f"execution-{index}"
        state.reduce("ExecutionStarted", {"execution_id": execution_id, "runtime": "python"})
        state.reduce("ExecutionExited", {"execution_id": execution_id, "exit_code": 0})
    for index in range(80):
        state.reduce(
            "VerificationCheckCompleted",
            {"criterion": f"check-{index}", "status": "passed"},
        )

    state.feed_stream("x" * (64 * 1024))

    assert len(state.tasks) == 128
    assert len(state.executions) == 256
    assert len(state.verification_checks) == 64
    assert len(state.stream_partial) <= 32 * 1024
    assert "truncated" in state.stream_partial


def test_workspace_tree_is_bounded_to_aperture_and_exposes_truthful_node_overflow_label():
    """Regression: an overlong workspace tree must fit inside the tree aperture and
    expose a truthful ``[N nodes]`` overflow/scroll affordance when the number of
    flattened tree rows exceeds the available row budget (height - 3).
    """
    from athena.cli.scene import OIScene, TreeNode
    from athena.cli.render.scene import render_scene_lines

    state = ProjectionState()

    # Build 60 flat workspace-tree nodes — far more than the ~20 row budget for
    # a height=24 aperture.  Each has a long label so we can confirm fit_cells
    # clamping works on the individual rows.
    workspace_tree: list[TreeNode] = []
    for i in range(60):
        workspace_tree.append(
            TreeNode(
                id=f"res-{i}",
                kind="resource",
                label=f"{'x' * 25}-file-{i:04d}",
                status="complete",
                children=(),
            )
        )

    scene = OIScene(
        viewport=Rect(0, 0, 50, 24),
        status="ok",
        title="TEST",
        mode=VisualActionKind.IDLE,
        workspace_tree=tuple(workspace_tree),
    )

    width, height = 50, 24
    split = max(width // 2, 18)  # 25
    tree_col_width = split - 1  # 24

    lines = render_scene_lines(
        state,
        scene,
        width=width,
        height=height,
        buddy_enabled=False,
    )

    # Invariant 1: no line exceeds the aperture width
    for idx, line in enumerate(lines):
        assert len(line) <= width, (
            f"Line {idx} exceeds aperture width {width}: len={len(line)} — {line!r}"
        )

    # Invariant 2: WORKSPACE MAP header in left half, bounded
    header_idx = None
    for idx, line in enumerate(lines):
        if "WORKSPACE MAP" in line:
            header_idx = idx
            break
    assert header_idx is not None, "WORKSPACE MAP header not found"

    header_line = lines[header_idx]
    split_pos = split
    left_header = header_line[:split_pos] if split_pos <= len(header_line) else header_line
    assert len(left_header.rstrip()) <= tree_col_width, (
        f"Left header exceeds tree column width: {left_header!r}"
    )

    # Invariant 3: tree rows fit inside the tree column width
    tree_start_row = header_idx + 1
    tree_rows_in_output = []
    for idx in range(tree_start_row, header_idx + 1 + 24):
        if idx < len(lines):
            tree_rows_in_output.append((idx, lines[idx]))

    assert len(tree_rows_in_output) > 1, (
        f"Expected multiple tree rows; got {len(tree_rows_in_output)}"
    )

    for row_idx, line in tree_rows_in_output:
        left_part = line[:split_pos] if split_pos <= len(line) else line
        assert len(left_part.rstrip()) <= tree_col_width, (
            f"Row {row_idx} tree content exceeds column width: {left_part!r}"
        )

    # Invariant 4: truthful [60 nodes] overflow label present
    found_label = False
    for row_idx, line in tree_rows_in_output:
        left_part = line[:split_pos] if split_pos <= len(line) else line
        stripped = left_part.rstrip()
        if stripped == "[60 nodes]" or stripped.endswith("[60 nodes]"):
            found_label = True
            break

    assert found_label, (
        f"[60 nodes] overflow label not found. "
        f"Lines: {[line_text for _, line_text in tree_rows_in_output]}"
    )


def test_native_projection_frame_preserves_buddy_character_owl():
    """Ensure the canonical character='owl' survives the Python→Rust bridge."""
    from athena.cli.native_bridge import native_projection_frame

    state = ProjectionState()
    frame = native_projection_frame(state)

    assert frame["buddy"]["character"] == "owl"
    assert frame["buddy"]["state"] == "idle"  # scene.mode.value, not state.status
    assert isinstance(frame["buddy"]["anchor"], str)


def test_native_projection_frame_custom_character_serializes():
    """A non-default character must be preserved through the bridge."""
    from athena.cli.native_bridge import native_projection_frame

    state = ProjectionState()
    frame = native_projection_frame(state, character="cat")

    assert frame["buddy"]["character"] == "cat"


def test_native_projection_frame_model_request_fields_present():
    """The model_request section of the frame must contain the expected keys."""
    from athena.cli.native_bridge import native_projection_frame

    state = ProjectionState()
    state.feed_stream("test line")
    state.seal_stream()

    frame = native_projection_frame(state)

    mr = frame["model_request"]
    assert "provider" in mr
    assert "model" in mr
    assert "role" in mr
    assert "request_id" in mr
    assert "status" in mr


def test_write_native_projection_outputs_valid_json_with_owl():
    """write_native_projection must emit parseable JSON with character='owl'."""
    from io import StringIO

    from athena.cli.native_bridge import write_native_projection

    state = ProjectionState()
    state.feed_stream("hello")
    state.seal_stream()

    buf = StringIO()
    write_native_projection(buf, state)
    output = buf.getvalue()

    import json

    frame = json.loads(output)
    assert frame["buddy"]["character"] == "owl"
    assert "layout" not in frame


def test_runtime_tree_keeps_task_operation_and_execution_hierarchy():
    state = ProjectionState()
    state.reduce("TaskStarted", {}, task_id="task-1")
    state.reduce(
        "CapabilityRequested",
        {"call_id": "call-1", "capability_id": "execute"},
        task_id="task-1",
    )
    state.reduce(
        "ExecutionStarted",
        {"call_id": "call-1", "execution_id": "exec-1", "runtime": "python"},
        task_id="task-1",
    )

    scene = build_oi_scene(state, Rect(0, 0, 80, 24))
    frame = native_projection_frame(state)
    by_id = {node.id: node for node in scene.entities}

    assert by_id["execution:exec-1"].metadata["parent_id"] == "call-1"
    runtime = frame["runtime_tree"]
    assert runtime[0]["id"] == "task:task-1"
    assert runtime[0]["children"][0]["id"] == "operation:call-1"
    assert runtime[0]["children"][0]["children"][0]["id"] == "execution:exec-1"
