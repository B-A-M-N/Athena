"""Stream window tests (UI mission §18/§32).

Guarantees: partial-line rejoin across chunks, carriage-return in-place
progress updates, ANSI hygiene, stdout/stderr distinction, bounded history
with drop accounting, and display-only truncation.
"""

from __future__ import annotations

from athena.cli.stream import StreamWindow, strip_ansi, truncate_ansi


def test_partial_lines_rejoin_across_chunks():
    w = StreamWindow()
    w.feed("hel")
    w.feed("lo wor")
    w.feed("ld\n")
    assert w.snapshot_text(5, 80)[-1] == "hello world"


def test_stderr_partial_rejoin_and_flagging():
    w = StreamWindow()
    w.feed("err frag", err=True)
    w.feed("ment\n", err=True)
    snap = w.snapshot(5, 80)
    line = [ln for ln in snap if ln.text][-1]
    assert line.text == "err fragment"
    assert line.err is True


def test_carriage_return_progress_updates_in_place():
    """CR progress: one line, final content — not one row per repaint."""
    w = StreamWindow()
    w.feed("downloading 10%")
    w.feed("\rdownloading 52%")
    w.feed("\rdownloading 100%\n")
    texts = [ln.text for ln in w.snapshot(5, 80) if ln.text]
    assert texts == ["downloading 100%"]


def test_carriage_return_overwrite_keeps_longer_tail():
    w = StreamWindow()
    w.feed("abcdef\rXY\n")
    assert w.snapshot_text(5, 80)[-1] == "XYcdef"


def test_cr_split_across_chunks():
    w = StreamWindow()
    w.feed("progress 1%")
    w.feed("\r")
    w.feed("progress 2%\n")
    texts = [ln.text for ln in w.snapshot(5, 80) if ln.text]
    assert texts == ["progress 2%"]


def test_ansi_sequences_stripped_for_display():
    w = StreamWindow()
    w.feed("\x1b[32mok\x1b[0m plain\n")
    assert w.snapshot_text(5, 80)[-1] == "ok plain"


def test_binary_control_bytes_replaced_not_crashing():
    w = StreamWindow()
    w.feed("good\x00\x07bad\x1f\n")
    line = w.snapshot_text(5, 80)[-1]
    assert "good" in line and "bad" in line
    assert "\x00" not in line


def test_feed_delta_never_duplicates_partial():
    """Model delta partials render as a VIEW only (P2-44 regression guard)."""
    w = StreamWindow()
    w.feed_delta("Hello")
    w.feed_delta(" world")
    first = w.snapshot_text(3, 80)
    second = w.snapshot_text(3, 80)
    assert first == second
    assert first[-1] == "Hello world"
    assert len([ln for ln in w.lines if ln.text]) == 0  # nothing committed
    w.seal_partial()
    assert w.snapshot_text(3, 80)[-1] == "Hello world"


def test_ring_bound_drops_oldest_and_counts():
    w = StreamWindow(max_lines=10)
    for i in range(25):
        w.feed(f"line {i}\n")
    assert len(w.lines) == 10
    assert w.dropped == 15
    texts = w.snapshot_text(10, 80)
    assert texts[-1] == "line 24"
    assert texts[0] == "line 15"


def test_snapshot_is_pure_and_padded():
    w = StreamWindow()
    w.feed("one\n")
    a = w.snapshot(4, 40)
    b = w.snapshot(4, 40)
    assert a == b
    assert len(a) == 4
    assert a[-1].text == "one"


def test_long_line_truncation_display_only():
    w = StreamWindow()
    long_line = "x" * 300
    w.feed(long_line + "\n")
    shown = w.snapshot_text(3, 40)[-1]
    assert len(shown) == 40
    assert shown.endswith("…")
    # committed content untouched
    assert len(w.lines[-1].text) == 300


def test_truncate_ansi_respects_width():
    assert truncate_ansi("hello", 10) == "hello"
    assert truncate_ansi("hello world", 5) == "hell…"
    assert truncate_ansi("hi", 1) == "…" or truncate_ansi("hi", 1) == "h"


def test_strip_ansi_removes_common_sequences():
    assert strip_ansi("\x1b[1;31mred\x1b[0m") == "red"
    assert strip_ansi("plain") == "plain"


def test_mixed_stdout_stderr_keep_flags():
    w = StreamWindow()
    w.feed("out line\n")
    w.feed("err line\n", err=True)
    snap = [ln for ln in w.snapshot(5, 80) if ln.text]
    assert snap[-2].err is False
    assert snap[-1].err is True


def test_huge_output_bounded_presentation():
    """10k lines: presentation bounded, no unbounded growth (§18/§30)."""
    w = StreamWindow(max_lines=100)
    for i in range(10_000):
        w.feed(f"row {i}\n")
    assert len(w.lines) == 100
    assert w.dropped == 9_900
    snap = w.snapshot(20, 60)
    assert len(snap) == 20
    assert snap[-1].text == "row 9999"


def test_empty_and_delayed_output():
    w = StreamWindow()
    w.feed("")
    w.feed_delta("")
    assert all(not ln.text for ln in w.snapshot(3, 40))
    w.feed("\n")  # empty line from process
    assert w.snapshot(3, 40)[-1].text == ""
