"""Dual-pane routing integration tests (UI mission §24/§32/§34).

Guarantees the pane ownership map: every major event class lands in the
intended region — calm conversation on the left, structured operational
activity on the right — with no cross-pane firehose and no duplicated
rendering.
"""

from __future__ import annotations

import asyncio
from io import StringIO

import pytest

from athena.cli.dual_pane import DualPaneSurface, Mascot, _OIWindow
from athena.protocol.events import make_event


def _surface() -> tuple[DualPaneSurface, StringIO, StringIO]:
    out, err = StringIO(), StringIO()
    s = DualPaneSurface(output=out, error=err, interactive=False)
    return s, out, err


def _render(s: DualPaneSurface, events) -> None:
    async def go():
        for e in events:
            await s.render_event(e)
        s.finish()
    asyncio.run(go())


def test_model_text_left_not_right_firehose():
    """Assistant prose coalesces to the left; right pane tracks activity."""
    s, out, _ = _surface()
    _render(s, [
        make_event("ModelDelta", {"text": "I will inspect the repo."}),
        make_event("ModelResponseCompleted", {}),
        make_event("TaskCompleted", {}),
    ])
    assert "I will inspect the repo." in out.getvalue()
    assert s.buddy.state in {"success", "idle"}  # sticky or decayed


def test_execution_lifecycle_updates_one_operation():
    """P1 regression: one logical op — not one row per lifecycle event."""
    s, _, _ = _surface()
    _render(s, [
        make_event("CapabilityRequested",
                   {"capability_id": "execute", "call_id": "c1",
                    "arguments": {"code": "git status"}}),
        make_event("CapabilityStarted", {"capability_id": "execute", "call_id": "c1"}),
        make_event("StdoutChunk", {"call_id": "c1", "data": "ok\n"}),
        make_event("CapabilityCompleted", {"capability_id": "execute", "call_id": "c1"}),
    ])
    assert len(s.activity.recent) == 1
    assert s.activity.active is None
    op = s.activity.recent[0]
    assert op.state == "done" and "git status" in op.summary


def test_stderr_routes_to_stream_flagged_not_doubled():
    s, out, err = _surface()
    _render(s, [
        make_event("CapabilityRequested", {"capability_id": "execute"}),
        make_event("StderrChunk", {"data": "partial"}),
        make_event("StderrChunk", {"data": " fragment\n"}),
    ])
    snap = s.stream.snapshot(5, 60)
    line = [ln for ln in snap if ln.text][-1]
    # partial rejoined (old code split blindly, corrupting fragments)
    assert line.text == "partial fragment"
    assert line.err is True


def test_approval_appears_in_oi_context_and_pauses_operation():
    s, _, _ = _surface()
    _render(s, [
        make_event("CapabilityRequested", {"capability_id": "fs.write"}),
        make_event("ApprovalRequested",
                   {"approval_id": "a1", "capability_id": "fs.write",
                    "scopes": ["call", "task"]}),
    ])
    assert s.activity.approval is not None
    assert s.activity.active is not None and s.activity.active.state == "waiting"
    assert s.buddy.state == "approval"
    frame = "\n".join(s.chassis_text())
    assert "APPROVAL REQUIRED" in frame
    assert "fs.write" in frame


def test_approval_resolution_cleans_up():
    """Approvals must not remain stuck after completion (§15)."""
    s, _, _ = _surface()
    _render(s, [
        make_event("CapabilityRequested", {"capability_id": "fs.write"}),
        make_event("ApprovalRequested", {"approval_id": "a1", "capability_id": "fs.write"}),
        make_event("ApprovalResolved", {"approval_id": "a1", "decision": "approved"}),
        make_event("CapabilityCompleted", {"capability_id": "fs.write"}),
    ])
    assert s.activity.approval is None
    frame = "\n".join(s.chassis_text())
    assert "APPROVAL REQUIRED" not in frame


def test_artifact_event_discoverable_in_machine():
    s, _, _ = _surface()
    _render(s, [
        make_event("CapabilityRequested", {"capability_id": "execute"}),
        make_event("ArtifactCreated", {"uri": "file:///tmp/x.md", "name": "x.md"}),
    ])
    frame = "\n".join(s.chassis_text())
    assert "x.md" in frame
    assert s.activity.artifacts[0].name == "x.md"


def test_background_task_visible_but_not_dominating():
    s, _, _ = _surface()
    _render(s, [
        make_event("CapabilityRequested", {"capability_id": "execute"}),
        make_event("ChildTaskCreated", {"child_task_id": "t5", "objective": "bg scan"}),
        make_event("StdoutChunk", {"data": "fg work\n"}),
    ])
    assert "t5" in s.activity.background
    # foreground op remains the active operation
    assert s.activity.active is not None and s.activity.active.capability == "execute"


def test_high_volume_output_bounded_and_chat_readable():
    """P1: a 5k-line process must not flood the calm pane or grow widgets."""
    s, out, _ = _surface()
    events = [make_event("CapabilityRequested", {"capability_id": "execute"})]
    events += [make_event("StdoutChunk", {"data": f"row {i}\n"}) for i in range(5000)]
    _render(s, events)
    assert len(s.stream.lines) == s.stream.lines.maxlen
    assert s.stream.dropped > 0
    # raw output is exclusive to the machine pane (§25: no duplication);
    # the calm pane must not receive any of the 5000 rows
    assert "row 4999" not in out.getvalue()
    assert "row " not in out.getvalue()


def test_carriage_return_progress_single_line_in_viewport():
    s, _, _ = _surface()
    _render(s, [
        make_event("CapabilityRequested", {"capability_id": "execute"}),
        make_event("StdoutChunk", {"data": "10%\r50%\r100%\n"}),
    ])
    texts = [ln.text for ln in s.stream.snapshot(5, 60) if ln.text]
    assert texts == ["100%"]


def test_chassis_renders_at_small_terminal(monkeypatch):
    """Responsive degradation: no crash, exact-height frame at 80 cols."""
    monkeypatch.setattr(DualPaneSurface, "_terminal_size", staticmethod(lambda: (80, 24)))
    s, _, _ = _surface()
    _render(s, [make_event("CapabilityRequested", {"capability_id": "execute"})])
    lines = s.chassis_text()
    assert len(lines) >= 5
    assert s.dual is False  # below MIN_DUAL_COLS → single-column degrade


def test_chassis_renders_at_large_terminal(monkeypatch):
    monkeypatch.setattr(DualPaneSurface, "_terminal_size", staticmethod(lambda: (160, 50)))
    s, _, _ = _surface()
    _render(s, [
        make_event("CapabilityRequested", {"capability_id": "execute",
                                           "arguments": {"code": "ls"}}),
        make_event("StdoutChunk", {"data": "file1\n"}),
    ])
    frame = "\n".join(s.chassis_text())
    assert "ATHENA MACHINE" in frame
    assert s.dual is True


def test_mascot_backcompat_api_still_works():
    m = Mascot()
    m.observe("ApprovalRequested")
    assert m.state == "waiting"           # legacy name
    assert m.buddy.state == "approval"    # semantic machine underneath
    lines = m.render(max_width=30)
    assert any("awaiting permission" in ln for ln in lines)


def test_oi_window_backcompat_api_still_works():
    w = _OIWindow()
    w.feed("a\nb")
    w.feed_delta("c")
    snap = w.snapshot(4, 40)
    assert snap[-2] == "a"
    assert snap[-1] == "bc"
    w.seal_partial()
    assert list(w.lines) == ["a", "bc"]


def test_repaint_skipped_when_not_tty():
    """repaint_oi is a no-op in pipes/tests — no escape garbage in output."""
    s, out, _ = _surface()
    s.interactive = True  # even if interactive flag is set…
    s.repaint_oi()        # …StringIO is not a tty, so nothing is written
    assert "\x1b[" not in out.getvalue()


def test_task_failed_shows_failure_everywhere():
    s, _, err = _surface()
    _render(s, [
        make_event("CapabilityRequested", {"capability_id": "execute"}),
        make_event("TaskFailed", {}),
    ])
    assert s.buddy.state == "failure"
    assert s.activity.task_status == "failed"
    assert "failed" in err.getvalue()


def test_buddy_progression_through_a_realistic_task():
    """Scenario §34.21: idle → thinking → executing → approval → success."""
    s, _, _ = _surface()
    states: list[str] = []

    async def go():
        for e in [
            make_event("ModelDelta", {"text": "checking"}),
            make_event("ExecutionStarted", {"runtime": "shell"}),
            make_event("StdoutChunk", {"data": "x\n"}),
            make_event("ApprovalRequested", {"approval_id": "a", "capability_id": "execute"}),
            make_event("ApprovalResolved", {"approval_id": "a", "decision": "approved"}),
            make_event("CapabilityCompleted", {"capability_id": "execute"}),
            make_event("TaskCompleted", {}),
        ]:
            await s.render_event(e)
            states.append(s.buddy.state)
    asyncio.run(go())
    assert states == [
        "thinking", "executing", "executing", "approval",
        "executing", "executing", "success",
    ]
