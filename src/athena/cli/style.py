"""Shared terminal styling primitives — Athena's small design system.

One home for the glyphs, box pieces, and ANSI accents both panes use, so the
calm conversation surface and the OI machine read as one product (UI mission
§31).  No framework: pure functions over strings.  Every helper is
width-honest — call sites pass display widths already computed via
``athena.cli.stream.display_width`` where escapes may be present.

Capability fallback: ``set_dumb(True)`` (or TERM=dumb / NO_COLOR) switches to
an ASCII glyph set and disables color so the interface stays coherent on
limited terminals (§9/§23).
"""

from __future__ import annotations

import os

__all__ = [
    "GLYPHS",
    "ascii_mode",
    "set_dumb",
    "glyph",
    "box_top",
    "box_bottom",
    "box_sep",
    "rule",
    "colorize",
    "status_glyph",
]

# Semantic status glyphs, ASCII fallback second.
_STATUS = {
    "running": ("▸", ">"),
    "pending": ("·", "."),
    "waiting": ("⏸", "||"),
    "done": ("✓", "ok"),
    "failed": ("✗", "!!"),
    "cancelled": ("⊘", "xx"),
    "approval": ("!", "!"),
    "artifact": ("◈", "*"),
    "background": ("⧉", "&"),
    "idle": ("○", "o"),
}

_UNICODE = {
    "tl": "┌", "tr": "┐", "bl": "└", "br": "┘",
    "h": "─", "v": "│", "ltee": "├", "rtee": "┤",
    "dtl": "╔", "dtr": "╗", "dbl": "╚", "dbr": "╝",
    "dh": "═", "dv": "║", "dltee": "╟", "drtee": "╢",
}
_ASCII = {
    "tl": "+", "tr": "+", "bl": "+", "br": "+",
    "h": "-", "v": "|", "ltee": "+", "rtee": "+",
    "dtl": "+", "dtr": "+", "dbl": "+", "dbr": "+",
    "dh": "=", "dv": "|", "dltee": "+", "drtee": "+",
}

GLYPHS = dict(_UNICODE)

_COLORS = {
    "dim": "\x1b[2m",
    "bold": "\x1b[1m",
    "err": "\x1b[31m",
    "ok": "\x1b[32m",
    "warn": "\x1b[33m",
    "accent": "\x1b[36m",
    "reset": "\x1b[0m",
}
_color_on = True


def _detect_dumb() -> bool:
    term = os.environ.get("TERM", "")
    return term in {"", "dumb"} or os.environ.get("NO_COLOR") is not None


def ascii_mode() -> bool:
    return GLYPHS["h"] == "-"


def set_dumb(dumb: bool | None = None) -> None:
    """Switch to ASCII glyphs + no color (auto-detect when ``dumb`` is None)."""
    global _color_on
    if dumb is None:
        dumb = _detect_dumb()
    GLYPHS.clear()
    GLYPHS.update(_ASCII if dumb else _UNICODE)
    _color_on = not dumb


def glyph(name: str) -> str:
    return GLYPHS.get(name, "?")


def status_glyph(status: str) -> str:
    pair = _STATUS.get(status, _STATUS["idle"])
    return pair[1] if ascii_mode() else pair[0]


def colorize(text: str, color: str) -> str:
    if not _color_on or color not in _COLORS:
        return text
    return f"{_COLORS[color]}{text}{_COLORS['reset']}"


def _fit(text: str, width: int) -> str:
    text = text[: max(width, 0)]
    return text


def box_top(title: str, width: int, *, double: bool = False) -> str:
    """Top border with an inline title plate: ``┌─ TITLE ────┐``."""
    p = ("dtl", "dtr", "dh") if double else ("tl", "tr", "h")
    inner = max(width - 2, 0)
    label = f" {title} " if title else ""
    label = _fit(label, inner)
    return glyph(p[0]) + label + glyph(p[2]) * (inner - len(label)) + glyph(p[1])


def box_sep(title: str, width: int, *, double: bool = False) -> str:
    """Interior section divider: ``╟── TITLE ──────╢``."""
    p = ("dltee", "drtee", "dh") if double else ("ltee", "rtee", "h")
    inner = max(width - 2, 0)
    fill = glyph(p[2])
    label = f"{fill * 2} {title} " if title else ""
    label = _fit(label, inner)
    return glyph(p[0]) + label + fill * (inner - len(label)) + glyph(p[1])


def box_bottom(width: int, *, double: bool = False) -> str:
    p = ("dbl", "dbr", "dh") if double else ("bl", "br", "h")
    return glyph(p[0]) + glyph(p[2]) * max(width - 2, 0) + glyph(p[1])


def rule(width: int, *, double: bool = False) -> str:
    return glyph("dh" if double else "h") * max(width, 0)


# Honour the environment at import time (TERM=dumb / NO_COLOR).
set_dumb()
