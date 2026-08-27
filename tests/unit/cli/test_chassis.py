"""Machine chassis rendering tests (UI mission §11/§23/§32).

Guarantees: framed title/status/sections at full size, deterministic
degradation as the terminal shrinks (decoration drops before content),
exact output line count, and no crashes at degenerate dimensions.
"""

from __future__ import annotations

import pytest

from athena.cli.activity import ActivityModel
from athena.cli.buddy import BuddyState
from athena.cli.chassis import ChassisView, render_chassis
from athena.cli.stream import StreamLine


def _view(*, approval: bool = False, running: bool = True) -> ChassisView:
    act = ActivityModel()
    if running:
        act.observe("CapabilityRequested",
                    {"capability_id": "execute", "arguments": {"code": "git status"}})
        act.observe("CapabilityStarted", {"capability_id": "execute"})
        act.observe("StdoutChunk", {"data": "on branch main\n"})
    if approval:
        act.observe("ApprovalRequested", {
            "approval_id": "a1", "capability_id": "fs.write",
            "scopes": ["call"], "reason": "policy",
        })
    stream = [StreamLine("$ git status"), StreamLine("on branch main")]
    return ChassisView(
        activity=act,
        buddy_lines=[" ,___,", " (O,O)", " /)_)"],
        buddy_state=BuddyState.EXECUTING,
        stream=stream,
    )


def test_full_frame_has_chassis_sections():
    lines = render_chassis(_view(), 64, 20)
    frame = "\n".join(lines)
    assert "ATHENA MACHINE" in frame
    assert "ACTIVE OPERATION" in frame
    assert "RECENT ACTIVITY" in frame or "OUTPUT" in frame
    assert "git status" in frame


def test_frame_is_exact_height_and_rectangular():
    w, h = 64, 18
    lines = render_chassis(_view(), w, h)
    assert len(lines) == h
    from athena.cli.stream import display_width
    for ln in lines:
        assert display_width(ln) == w


def test_status_strip_answers_current_activity():
    lines = render_chassis(_view(), 64, 18)
    assert "EXECUTING" in lines[1]
    assert "git status" in lines[1]


def test_approval_card_visible_and_contextual():
    lines = render_chassis(_view(approval=True), 64, 20)
    frame = "\n".join(lines)
    assert "AUTHORIZATION" in frame
    assert "APPROVAL REQUIRED" in frame
    assert "fs.write" in frame
    assert "deny" in frame
    # surrounding operational context is preserved
    assert "git status" in frame


def test_small_height_drops_decoration_before_content():
    lines = render_chassis(_view(approval=True), 64, 8)
    frame = "\n".join(lines)
    # approval (most important) survives; lower sections are dropped
    assert "APPROVAL REQUIRED" in frame
    assert len(lines) == 8


def test_narrow_width_hides_buddy_column():
    wide = render_chassis(_view(), 64, 20)
    narrow = render_chassis(_view(), 40, 20)
    wide_text = "\n".join(wide)
    narrow_text = "\n".join(narrow)
    assert "(O,O)" in wide_text          # buddy visible
    assert "(O,O)" not in narrow_text     # buddy column collapsed
    assert "git status" in narrow_text    # content preserved


def test_degenerate_dimensions_never_crash():
    for w, h in [(10, 2), (1, 1), (20, 3), (27, 4), (0, 0)]:
        lines = render_chassis(_view(), w, h)
        assert len(lines) == h


@pytest.mark.parametrize("width,height", [(28, 5), (40, 10), (64, 14), (100, 24)])
def test_all_sizes_produce_exact_height(width, height):
    lines = render_chassis(_view(), width, height)
    assert len(lines) == height


def test_idle_machine_not_blank_or_broken():
    act = ActivityModel()
    view = ChassisView(activity=act, buddy_lines=[" ,___,"], buddy_state=BuddyState.IDLE,
                       stream=[])
    lines = render_chassis(view, 60, 12)
    frame = "\n".join(lines)
    assert "ATHENA MACHINE" in frame
    assert "idle" in frame.lower()


def test_dropped_counter_shown_in_status():
    v = _view()
    v.dropped = 42
    lines = render_chassis(v, 64, 18)
    assert "-42" in lines[1]
