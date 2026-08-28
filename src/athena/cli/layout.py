"""Geometry for the Athena instrument chassis.

Layout is deliberately independent from content.  In particular, the buddy,
approval card, history mode, and live stream never get to change the size of
either aperture.  This keeps the operator and OI surfaces visually equal and
makes resize behavior deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DisplayMode(StrEnum):
    AUTO = "auto"
    GLASS = "glass"
    ANSI = "ansi"
    PLAIN = "plain"


class LayoutMode(StrEnum):
    GLASS_FULL = "glass-full"
    GLASS_COMPACT = "glass-compact"
    ANSI_INSTRUMENT = "ansi-instrument"
    PLAIN = "plain"


@dataclass(frozen=True)
class Rect:
    """A terminal-cell rectangle."""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.right and self.y <= y < self.bottom


@dataclass(frozen=True)
class AthenaLayout:
    """Complete chassis geometry for one terminal size."""

    columns: int
    rows: int
    mode: LayoutMode
    chassis: Rect
    header: Rect
    operator: Rect
    oi: Rect
    controls: Rect
    prompt: Rect

    @property
    def apertures_equal(self) -> bool:
        return self.operator.width == self.oi.width and self.operator.height == self.oi.height


def _mode(columns: int, rows: int, requested: str | DisplayMode) -> LayoutMode:
    try:
        requested = DisplayMode(requested)
    except ValueError:
        # Configuration and environment values are user-editable.  A typo
        # must not prevent Athena from starting; AUTO is the safe projection.
        requested = DisplayMode.AUTO
    if requested is DisplayMode.PLAIN:
        return LayoutMode.PLAIN
    if requested is DisplayMode.ANSI:
        return LayoutMode.ANSI_INSTRUMENT
    if requested is DisplayMode.GLASS:
        return LayoutMode.GLASS_FULL if columns >= 140 and rows >= 36 else LayoutMode.GLASS_COMPACT
    if columns >= 140 and rows >= 36:
        return LayoutMode.GLASS_FULL
    if columns >= 110 and rows >= 30:
        return LayoutMode.GLASS_COMPACT
    if columns >= 90 and rows >= 20:
        return LayoutMode.ANSI_INSTRUMENT
    return LayoutMode.PLAIN


def compute_layout(
    columns: int,
    rows: int,
    requested: str | DisplayMode = DisplayMode.AUTO,
) -> AthenaLayout:
    """Compute a stable equal-aperture chassis.

    The compact/ANSI thresholds are policy, not assumptions in the renderer:
    full Glass is intended for approximately 140x36 and above, compact Glass
    for 110x30, ANSI for 90x20, and line mode below that.
    """
    columns = max(int(columns), 1)
    rows = max(int(rows), 1)
    mode = _mode(columns, rows, requested)

    if mode is LayoutMode.PLAIN:
        empty = Rect(0, 0, columns, rows)
        return AthenaLayout(columns, rows, mode, empty, empty, empty, empty, empty, empty)

    margin = 2 if columns >= 100 else 1
    gap = 3
    header_height = 3
    rail_height = 4
    aperture_height = max(rows - header_height - rail_height, 1)
    available = max(columns - (margin * 2) - gap, 2)
    aperture_width = max(1, available // 2)
    # An odd cell remains in the seam/margin, never in one pane.
    left_x = margin
    right_x = left_x + aperture_width + gap
    chassis = Rect(0, 0, columns, rows)
    header = Rect(margin, 0, columns - margin * 2, header_height)
    operator = Rect(left_x, header_height, aperture_width, aperture_height)
    oi = Rect(right_x, header_height, aperture_width, aperture_height)
    controls = Rect(margin, header_height + aperture_height, columns - margin * 2, 2)
    prompt = Rect(margin, controls.bottom, columns - margin * 2, max(rows - controls.bottom, 1))
    return AthenaLayout(columns, rows, mode, chassis, header, operator, oi, controls, prompt)


__all__ = ["AthenaLayout", "DisplayMode", "LayoutMode", "Rect", "compute_layout"]
