"""Retro machine chassis — the OI pane's operator-console frame (§11–§12).

A pure, deterministic renderer: ``render_chassis(view, width, height)`` takes
a view-model (activity model + buddy + stream snapshot) and returns the exact
lines of the framed right pane.  No state, no I/O — every layout decision is
a function of (view, width, height), so responsive degradation (§23) is
exhaustively testable.

Visual identity: retro intelligent machine / operator console.  A double-line
chassis with a title plate, a status strip, inset ACTIVE OPERATION and RECENT
ACTIVITY sections, a bounded output viewport, and the reactive buddy in its
own column.  Decoration shrinks first as space tightens; content never does.

Degradation ladder (as height shrinks):
    full → drop buddy speech → shrink buddy → drop recent activity
    → drop output viewport → title + status strip only
Width ladder (as width shrinks):
    buddy column beside stream → buddy hidden → ASCII glyphs → single column
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from athena.cli import style
from athena.cli.activity import ActivityModel, OpState
from athena.cli.stream import StreamLine, truncate_ansi

__all__ = ["ChassisView", "render_chassis", "MIN_WIDTH", "BUDDY_WIDTH"]

MIN_WIDTH = 28        # below this the chassis collapses to plain tagged lines
BUDDY_WIDTH = 20      # buddy column width when shown


@dataclass
class ChassisView:
    """Everything the chassis needs for one frame (all derived, read-only)."""

    activity: ActivityModel
    buddy_lines: list[str]
    buddy_state: str
    stream: list[StreamLine]      # bounded output viewport, newest last
    dropped: int = 0              # stream lines elided by the ring bound
    approvals_inline: bool = True


def _fit(text: str, width: int) -> str:
    return truncate_ansi(text, width)


def _op_line(op: Any, width: int, *, verbose: bool) -> list[str]:
    g = style.status_glyph(op.state)
    head = f"{g} {op.capability}"
    if op.summary:
        head += f" · {op.summary}"
    lines = [_fit(head, width)]
    if verbose and op.detail:
        lines.append(_fit(f"  {op.detail}", width))
    return lines


def _approval_lines(view: ChassisView, width: int) -> list[str]:
    ap = view.activity.approval
    assert ap is not None
    g = style.status_glyph("approval")
    out = [_fit(f"{g} APPROVAL REQUIRED · {ap.capability}", width)]
    if ap.reason:
        out.append(_fit(f"  {ap.reason}", width))
    scopes = " / ".join(f"{i + 1}:{s}" for i, s in enumerate(ap.scopes[:4]))
    out.append(_fit(f"  [{scopes} / d:deny]", width))
    return out


def render_chassis(view: ChassisView, width: int, height: int) -> list[str]:
    """Render the framed OI machine into exactly ``height`` lines.

    Lines are display-width-exact (padded/truncated to ``width``); ANSI color
    is only added where display width is unaffected.
    """
    if width < MIN_WIDTH or height < 3:
        # Degenerate: tagged plain lines, no frame (still truthful).
        return _render_degenerate(view, width, height)

    act = view.activity
    inner = width - 2  # between the chassis verticals
    pad = lambda s: s + " " * max(inner - len(s), 0)
    edge = lambda s: style.glyph("dv") + pad(_fit(s, inner)) + style.glyph("dv")
    sep_edge = lambda s: (
        style.glyph("dltee") + pad(_fit(s, inner)) + style.glyph("drtee"))

    # ---------------- title plate + status strip ----------------
    title = style.box_top("ATHENA MACHINE", width, double=True)
    status = _status_strip(view, inner)
    rows: list[str] = [title, edge(status)]

    budget = height - 2 - 1  # minus title/status and bottom border
    sections: list[list[str]] = []

    # ---------------- approval card (highest priority inset) --------------
    if act.approval is not None and view.approvals_inline:
        card = [sep_edge("AUTHORIZATION")]
        card += [edge(ln) for ln in _approval_lines(view, inner)]
        sections.append(card)

    # ---------------- active operation ------------------------------------
    active_lines: list[str] = []
    if act.active is not None:
        op = act.active
        active_lines = _op_line(op, inner - 2, verbose=True)
        # output viewport tail for the running op
        tail = list(op.output_tail)
        if op.output_dropped:
            tail.insert(0, f"… {op.output_dropped} earlier lines")
        for ln in tail[-3:]:
            active_lines.append(_fit(f"  {style.glyph('v')} {ln}", inner - 2))
    elif act.approval is None:
        label = act.current_label()
        active_lines = [_fit(f"{style.status_glyph('idle')} {label}", inner - 2)]
    if active_lines:
        sec = [sep_edge("ACTIVE OPERATION")]
        sec += [edge(ln) for ln in active_lines]
        sections.append(sec)

    # ---------------- stream viewport (with buddy column) -----------------
    stream_rows = _stream_rows(view, inner, budget)
    if stream_rows:
        sections.append([sep_edge("OUTPUT")] + stream_rows)

    # ---------------- recent activity + artifacts + background ------------
    trail: list[str] = []
    for op in list(act.recent)[:3]:
        trail += _op_line(op, inner - 2, verbose=False)
    for art in list(act.artifacts)[:2]:
        g = style.status_glyph("artifact")
        label = f"{g} {art.name + ': ' if art.name else ''}{art.ref}"
        trail.append(_fit(label, inner - 2))
    for bg in list(act.background.values())[:2]:
        if bg.state == "done":
            continue
        g = style.status_glyph("background")
        trail.append(_fit(f"{g} {bg.label} · {bg.state}", inner - 2))
    if trail:
        sec = [sep_edge("RECENT ACTIVITY")]
        sec += [edge(ln) for ln in trail]
        sections.append(sec)

    # ---------------- assemble within budget (decoration shrinks first) ---
    bottom = style.box_bottom(width, double=True)
    body: list[str] = []
    # Sections in priority order: approval, active, stream, trail.  Lower
    # sections are dropped wholesale when space runs out; the first (most
    # important) section is truncated rather than dropped entirely.
    for sec in sections:
        remaining = budget - len(body)
        if remaining <= 0:
            break
        if len(sec) <= remaining:
            body += sec
        elif not body:
            body += sec[:remaining]
        else:
            break
    while len(body) < budget:
        body.append(edge(""))
    return rows + body + [bottom]


def _status_strip(view: ChassisView, inner: int) -> str:
    act = view.activity
    state = view.buddy_state.upper()
    now = act.current_label()
    strip = f"[{state}] {now}"
    extras: list[str] = []
    if act.approval is not None:
        extras.append("!approval")
    attn = sum(1 for b in act.background.values() if b.needs_attention)
    if attn:
        extras.append(f"{attn} bg!")
    if view.dropped:
        extras.append(f"-{view.dropped}")
    if extras:
        strip += "  " + " ".join(extras)
    return _fit(strip, inner)


def _stream_rows(view: ChassisView, inner: int, budget: int) -> list[str]:
    """Bounded output viewport; buddy rides in a right-hand column."""
    if not view.stream:
        return []
    buddy = [bl for bl in view.buddy_lines if bl.strip()]
    show_buddy = bool(buddy) and inner >= BUDDY_WIDTH + 24
    stream_w = inner - (BUDDY_WIDTH + 1 if show_buddy else 0)
    if stream_w < 12:
        show_buddy, stream_w = False, inner
    # Reserve a sane share of the budget for the viewport.
    rows_wanted = min(len(view.stream), max(budget // 2, 2), 6)
    snap = view.stream[-rows_wanted:]
    pad_top = max((len(snap) - len(buddy)) // 2, 0) if show_buddy else 0
    out: list[str] = []
    edge_l = style.glyph("dv")
    edge_r = style.glyph("dv")
    for i in range(rows_wanted):
        ln = snap[i]
        text = truncate_ansi(ln.text, stream_w)
        if ln.err:
            text = style.colorize(text, "err")
        cell = text + " " * max(stream_w - len(ln.text), 0)
        if len(ln.text) > stream_w:
            cell = text
        if show_buddy:
            bi = i - pad_top
            bcell = buddy[bi] if 0 <= bi < len(buddy) else ""
            bcell = _fit(bcell, BUDDY_WIDTH)
            bcell = bcell + " " * max(BUDDY_WIDTH - len(bcell), 0)
            out.append(f"{edge_l}{cell} {bcell}{edge_r}")
        else:
            out.append(f"{edge_l}{cell}{' ' * max(inner - stream_w, 0)}{edge_r}")
    return out


def _render_degenerate(view: ChassisView, width: int, height: int) -> list[str]:
    """No room for a frame: plain tagged lines, newest last."""
    if height <= 0:
        return []
    act = view.activity
    lines = [f"[{view.buddy_state}] {act.current_label()}"]
    if act.approval is not None:
        lines.append(f"! approval: {act.approval.capability}")
    for ln in view.stream[-max(height - len(lines) - 1, 0):]:
        lines.append(("! " if ln.err else "") + ln.text)
    lines = [truncate_ansi(ln, width) for ln in lines]
    if len(lines) >= height:
        return lines[-height:]
    return [""] * (height - len(lines)) + lines
