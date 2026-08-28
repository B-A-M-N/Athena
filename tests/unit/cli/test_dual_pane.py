"""Semantic and layout regression tests for the retained operator surface."""

from __future__ import annotations

from io import StringIO

import pytest

from athena.cli.dual_pane import DualPaneSurface, Mascot, _OIWindow
from athena.protocol.events import make_event


class _TTY(StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def tty_surface(monkeypatch):
    monkeypatch.setattr(
        DualPaneSurface,
        "_terminal_size",
        staticmethod(lambda: (120, 40)),
    )
    output = _TTY()
    return DualPaneSurface(output=output, error=output, interactive=True), output


@pytest.mark.asyncio
async def test_conversation_and_operations_have_separate_owners(tty_surface):
    surface, _ = tty_surface
    surface.render_user_message("Please inspect the repository.")
    await surface.render_event(
        make_event("ModelDelta", {"text": "I am checking the repository now."})
    )
    await surface.render_event(
        make_event(
            "CapabilityRequested",
            {
                "call_id": "call-1",
                "capability_id": "execute",
                "arguments": {"language": "shell", "code": "pytest -q"},
            },
        )
    )
    await surface.render_event(
        make_event("CapabilityStarted", {"call_id": "call-1", "capability_id": "execute"})
    )
    await surface.render_event(
        make_event("StdoutChunk", {"execution_id": "missing-map", "data": "ok\n"})
    )

    screen = "\n".join(surface._frame_lines())
    assert "YOU     Please inspect the repository." in screen
    assert "ATHENA  I am checking the repository now." in screen
    assert "ACTIVE OPERATION" in screen
    assert "execute  RUNNING" in screen
    assert "RECENT ACTIVITY" in screen
    # Internal event names do not become a left-side firehose.
    assert "CapabilityRequested" not in screen
    # The surface is only a renderer: operation identity and lifecycle state
    # are the shared projection consumed by every frontend.
    assert surface._operations is surface.projection.operations
    assert surface._operations["call-1"] is surface.projection.operations["call-1"]
    assert surface.projection.active_operation_id == "call-1"


@pytest.mark.asyncio
async def test_event_reduction_does_not_duplicate_stream_chunks(tty_surface):
    surface, _ = tty_surface
    await surface.render_event(
        make_event(
            "CapabilityRequested",
            {"call_id": "call-stream", "capability_id": "execute"},
        )
    )
    await surface.render_event(
        make_event("StdoutChunk", {"call_id": "call-stream", "data": "hello\n"})
    )

    operation = surface.projection.operations["call-stream"]
    assert list(operation.output) == ["hello"]
    assert list(surface.projection.stream)[-1] == "hello"


@pytest.mark.asyncio
async def test_approval_is_embedded_with_context_and_does_not_replace_chat(tty_surface):
    surface, _ = tty_surface
    surface.render_user_message("Run the migration, but show me what needs approval.")
    await surface.render_event(
        make_event(
            "CapabilityRequested",
            {
                "call_id": "call-2",
                "capability_id": "execute",
                "arguments": {"code": "python migrate.py", "path": "migrate.py"},
            },
        )
    )
    await surface.render_event(
        make_event(
            "ApprovalRequested",
            {
                "call_id": "call-2",
                "approval_id": "approval-2",
                "capability_id": "execute",
                "scopes": ["call", "task"],
                "reason": "running a migration changes repository state",
            },
        )
    )

    screen = "\n".join(surface._frame_lines())
    assert "Run the migration" in screen
    assert "APPROVAL" in screen
    assert "Approval required" in screen
    assert "migrate.py" in screen
    # Long approval context wraps within the machine pane instead of being
    # clipped or pushed into the conversation transcript.
    assert "reason  running a migration" in screen
    assert "changes repository state" in screen
    assert "keys  1:call 2:task d:deny" in screen
    assert surface._pending_approval["approval_id"] == "approval-2"


@pytest.mark.asyncio
async def test_approval_summary_does_not_erase_actionable_context(tty_surface):
    surface, _ = tty_surface
    await surface.render_event(
        make_event(
            "CapabilityRequested",
            {
                "call_id": "call-3",
                "capability_id": "write_file",
                "arguments": {"path": "notes.md", "content": "hello"},
            },
        )
    )
    await surface.render_event(
        make_event(
            "ApprovalRequested",
            {
                "call_id": "call-3",
                "approval_id": "approval-3",
                "capability_id": "write_file",
                "scopes": ["call", "session"],
                "reason": "writing the file changes the workspace",
            },
        )
    )
    await surface.render_event(make_event("ApprovalRequested", {"calls": 1}))

    assert surface._pending_approval["approval_id"] == "approval-3"
    screen = "\n".join(surface._frame_lines())
    assert "reason  writing the file" in screen
    assert "changes the workspace" in screen
    assert "keys  1:call 2:session d:deny" in screen


@pytest.mark.asyncio
async def test_completed_operations_move_to_history_and_keep_artifacts(tty_surface):
    surface, _ = tty_surface
    await surface.render_event(
        make_event(
            "CapabilityRequested",
            {
                "call_id": "call-4",
                "capability_id": "execute",
                "arguments": {"code": "python build.py", "path": "build.py"},
            },
        )
    )
    await surface.render_event(
        make_event("CapabilityStarted", {"call_id": "call-4", "capability_id": "execute"})
    )
    await surface.render_event(
        make_event(
            "ExecutionStarted",
            {"call_id": "call-4", "execution_id": "exec-4", "runtime": "python"},
        )
    )
    await surface.render_event(
        make_event("ExecutionExited", {"execution_id": "exec-4", "exit_code": 0})
    )
    await surface.render_event(
        make_event("ArtifactCreated", {"call_id": "call-4", "uri": "artifact://build-report"})
    )

    screen = "\n".join(surface._frame_lines())
    assert "· no capability is running" in screen
    assert "OPERATION HISTORY" in screen
    assert "execute  COMPLETE" in screen
    assert "artifact://build-report" in screen


@pytest.mark.asyncio
async def test_right_scroll_is_independent_and_live_output_does_not_reset_it(tty_surface):
    surface, _ = tty_surface
    await surface.render_event(
        make_event(
            "CapabilityRequested",
            {
                "call_id": "call-scroll",
                "capability_id": "execute",
                "arguments": {"code": "tail -f log"},
            },
        )
    )
    await surface.render_event(
        make_event("CapabilityStarted", {"call_id": "call-scroll", "capability_id": "execute"})
    )
    for index in range(24):
        await surface.render_event(
            make_event("StdoutChunk", {"call_id": "call-scroll", "data": f"line-{index}\n"})
        )

    assert surface.scroll("right", 5) is True
    await surface.render_event(
        make_event("StdoutChunk", {"call_id": "call-scroll", "data": "new-live-line\n"})
    )
    assert surface._right_scroll == 5
    assert "OI // HISTORY" in "\n".join(surface._frame_lines())
    assert surface.scroll_to_bottom("right") is True
    assert surface._right_scroll == 0


@pytest.mark.asyncio
async def test_search_and_background_events_are_intentionally_projected(tty_surface):
    surface, _ = tty_surface
    await surface.render_event(make_event("SearchStarted", {"query": "approval lifecycle"}))
    assert surface._status == "SEARCHING"
    assert any(text == "Search · approval lifecycle" for _, text in surface._recent)

    await surface.render_event(make_event("BackgroundTaskStarted", {}))
    assert surface._status == "DELEGATED"
    await surface.render_event(make_event("BackgroundTaskFailed", {}))
    screen = "\n".join(surface._frame_lines())
    assert "Background work failed" in screen
    assert surface.mascot.state == "failure"


def test_stream_projection_handles_ansi_carriage_returns_and_partial_lines():
    window = _OIWindow(max_lines=3)
    window.feed("\x1b[32mprogress 10%\rprogress 100%\x1b[0m\nlast")
    assert window.snapshot(4, 40)[-2:] == ["progress 100%", "last"]
    window.feed(" line\n")
    assert window.snapshot(2, 40)[-1] == "last line"


@pytest.mark.parametrize(
    ("event_type", "payload", "expected"),
    [
        ("ModelRequestStarted", {}, "thinking"),
        ("ModelDelta", {"text": "hi"}, "responding"),
        ("ModelRequestFailed", {"error": "provider unavailable"}, "failure"),
        ("ContextCompressed", {}, "inspecting"),
        ("CapabilityRequested", {"capability_id": "execute"}, "coding"),
        ("ApprovalRequested", {}, "approval"),
        ("TaskStateChanged", {"status": "WAITING_INPUT"}, "waiting"),
        ("MutationRecordFailed", {}, "failure"),
        ("ChildTaskCreated", {}, "delegated"),
        ("TaskCompleted", {}, "success"),
        ("TaskPartial", {}, "warning"),
        ("TaskFailed", {}, "failure"),
        ("TaskCancelled", {}, "interrupted"),
        ("TaskInterrupted", {}, "interrupted"),
    ],
)
def test_mascot_state_mapping_is_semantic(event_type, payload, expected):
    mascot = Mascot()
    mascot.observe(event_type, payload)
    assert mascot.state == expected
    assert mascot.render(max_width=24)


def test_frame_degrades_to_single_column_when_terminal_is_narrow(monkeypatch):
    monkeypatch.setattr(
        DualPaneSurface,
        "_terminal_size",
        staticmethod(lambda: (72, 12)),
    )
    surface = DualPaneSurface(output=_TTY(), interactive=True)
    assert surface.dual is False
    assert surface._full_screen is False


def test_instrument_frame_is_one_chassis_with_inset_apertures(tty_surface):
    surface, _ = tty_surface
    lines = surface._frame_lines()
    aperture_top = lines[surface.layout.operator.y]
    aperture_bottom = lines[surface.layout.operator.bottom - 1]

    assert aperture_top.count("╭") == 1
    assert aperture_top.count("╮") == 1
    assert aperture_bottom.count("╰") == 1
    assert aperture_bottom.count("╯") == 1
    assert "┬" not in aperture_top
    assert "┴" not in aperture_bottom
    # The inset seam is cabinet relief, not another boxed pane.
    assert "░" in lines[surface.layout.operator.y + 1]
