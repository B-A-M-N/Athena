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
class ChromeMetrics:
    """Rows and cells reserved by the physical instrument shell.

    The hierarchy is: outer chassis (exterior_margin) → per-aperture
    ``aperture_rim`` (one cell) → inner viewport.  The chassis top and bottom
    rows sit at *y = 0* and *y = rows - 1* so every row/column is accounted
    for.  ``outer_operator`` and ``outer_oi`` are available on the layout for
    renderers that draw the two-layer box structure.

    ``operator`` and ``oi`` rectangles are the *inner* aperture viewports.
    Their one-cell rims are represented by ``aperture_rim`` and are reserved
    here, so renderers never need to subtract shell chrome a second time.
    """

    exterior_margin: int = 2
    pane_gap: int = 3
    header_rows: int = 2
    aperture_rim: int = 1
    rail_rows: int = 4
    prompt_rows: int = 2

    @property
    def aperture_rim_rows(self) -> int:
        return self.aperture_rim * 2

    @property
    def exterior_perimeter_rows(self) -> int:
        """Rows the outer chassis border occupies at top and bottom (always 2)."""
        return 2


@dataclass(frozen=True)
class AthenaLayout:
    """Complete chassis geometry for one terminal size.

    ``outer_operator`` and ``outer_oi`` include the one-cell aperture rim.
    Renderers use them to draw the two-layer box: ``╔═╗`` chassis outer +
    ``╭─╮`` inner rim.
    """

    columns: int
    rows: int
    mode: LayoutMode
    chassis: Rect
    header: Rect
    operator: Rect
    oi: Rect
    outer_operator: Rect
    outer_oi: Rect
    controls: Rect
    prompt: Rect
    chrome: ChromeMetrics = ChromeMetrics()

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
    # Below this size there is not enough room for a perimeter, two rims, a
    # control rail, and a prompt.  Degrade to the line projection instead of
    # returning overlapping rectangles, even when a renderer was requested
    # explicitly.
    if columns < 80 or rows < 18:
        return LayoutMode.PLAIN
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
        return AthenaLayout(
            columns, rows, mode, empty, empty, empty, empty, empty, empty, empty, empty
        )

    chrome = ChromeMetrics(exterior_margin=2 if columns >= 100 else 1)
    margin = chrome.exterior_margin
    available = max(columns - (margin * 2) - chrome.pane_gap, 2)
    outer_aperture_width = max(2 * chrome.aperture_rim + 1, available // 2)
    # An odd cell remains in the seam/margin, never in one pane.
    inner_width = max(1, outer_aperture_width - chrome.aperture_rim * 2)
    outer_aperture_height = max(
        2 * chrome.aperture_rim + 1,
        rows - 2 - chrome.header_rows - chrome.rail_rows - chrome.prompt_rows,
    )
    inner_height = max(1, outer_aperture_height - chrome.aperture_rim_rows)
    chassis = Rect(0, 0, columns, rows)
    header = Rect(margin, 1, columns - margin * 2, chrome.header_rows)
    left_outer_x = margin
    right_outer_x = left_outer_x + outer_aperture_width + chrome.pane_gap
    content_y = header.bottom + chrome.aperture_rim
    operator = Rect(left_outer_x + chrome.aperture_rim, content_y, inner_width, inner_height)
    oi = Rect(right_outer_x + chrome.aperture_rim, content_y, inner_width, inner_height)
    controls_y = operator.bottom + chrome.aperture_rim
    controls = Rect(margin, controls_y, columns - margin * 2, chrome.rail_rows)
    prompt = Rect(
        margin,
        controls.bottom,
        columns - margin * 2,
        max(rows - controls.bottom - 1, chrome.prompt_rows),
    )
    # At supported dimensions the prompt ends immediately above the outer
    # bottom perimeter.  Keep the clamp defensive for explicit tiny inputs.
    if prompt.bottom > rows - 1:
        prompt = Rect(prompt.x, prompt.y, prompt.width, max(rows - 1 - prompt.y, 1))
    # Outer aperture rectangles include the one-cell rim.
    outer_operator = Rect(
        left_outer_x, content_y - chrome.aperture_rim, outer_aperture_width, outer_aperture_height
    )
    outer_oi = Rect(
        right_outer_x, content_y - chrome.aperture_rim, outer_aperture_width, outer_aperture_height
    )
    return AthenaLayout(
        columns,
        rows,
        mode,
        chassis,
        header,
        operator,
        oi,
        outer_operator,
        outer_oi,
        controls,
        prompt,
        chrome,
    )


__all__ = [
    "AthenaLayout",
    "ChromeMetrics",
    "DisplayMode",
    "LayoutMode",
    "Rect",
    "compute_layout",
]
