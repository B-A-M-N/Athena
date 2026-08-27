"""Screen-emulation tests for terminal_session (pyte framebuffer) + list op."""

from __future__ import annotations

import pytest

from athena.capabilities.terminal_session import (
    TerminalSessionCapability,
    feed_screen,
    new_screen,
)
from athena.protocol.capabilities import CapabilityRequest


def _req(op: str, task_id=None, **args):
    return CapabilityRequest(
        capability_id="terminal_session",
        arguments={"operation": op, **args},
        task_id=task_id,
    )


@pytest.fixture
def term():
    cap = TerminalSessionCapability()
    yield cap
    cap.close_all()


# -- pyte framebuffer feeding logic (pure, no PTY) -------------------------

@pytest.mark.athena_scenario("BODY-002")
def test_overwrite_moves_cursor_and_replaces():
    s = new_screen(4, 10)
    feed_screen(s, "abcdefghij")
    # carriage return + rewrite the first 3 chars
    feed_screen(s, "\rXYZ")
    lines = list(s.display)
    assert lines[0] == "XYZdefghij"
    assert (s.cursor.y, s.cursor.x) == (0, 3)


def test_newline_advances_row():
    s = new_screen(4, 10)
    feed_screen(s, "one\r\ntwo\r\nthree")
    assert [ln.rstrip() for ln in s.display[:3]] == ["one", "two", "three"]
    assert s.cursor.y == 2


def test_clear_sequence_erases_screen():
    s = new_screen(4, 10)
    feed_screen(s, "stale content here\r\nmore")
    feed_screen(s, "\x1b[2J\x1b[H")  # clear + home
    assert all(ln.strip() == "" for ln in s.display)


def test_alternate_screen_overwrite_via_cursor_reposition():
    # progress-bar style redraw: \r + partial overwrite
    s = new_screen(2, 8)
    feed_screen(s, "0%")
    feed_screen(s, "\r50%")
    feed_screen(s, "\r100%")
    assert s.display[0].rstrip() == "100%"
    assert len(s.display) == 2


@pytest.mark.athena_scenario("BODY-002")
def test_scrolling_keeps_last_rows():
    s = new_screen(3, 5)
    feed_screen(s, "a\r\nb\r\nc\r\nd")
    assert [ln.strip() for ln in s.display] == ["b", "c", "d"]


# -- capability-level: list op works without a session arg -----------------

async def test_list_empty_sessions_ok_without_session_arg(term):
    r = await term.invoke(_req("list", task_id="t9"))
    assert r.error is None
    assert "0 session" in (r.output or "")
    assert r.metadata.get("sessions") == []
