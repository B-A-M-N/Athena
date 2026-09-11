#!/usr/bin/env python3
"""Check coarse visual invariants for a deterministic 384x256 OI dump."""

from __future__ import annotations

import subprocess
import sys


WIDTH = 384
HEIGHT = 256


def fail(message: str) -> None:
    raise SystemExit(f"native-oi-visual: FAIL — {message}")


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} OI.png", file=sys.stderr)
        return 2
    try:
        raw = subprocess.check_output(
            ["convert", sys.argv[1], "-depth", "8", "rgba:-"], stderr=subprocess.DEVNULL
        )
    except (OSError, subprocess.CalledProcessError) as error:
        fail(f"could not decode OI dump: {error}")
    expected = WIDTH * HEIGHT * 4
    if len(raw) != expected:
        fail(f"decoded OI dump has {len(raw)} bytes, expected {expected}")

    def pixel(x: int, y: int) -> tuple[int, int, int, int]:
        offset = (y * WIDTH + x) * 4
        return tuple(raw[offset : offset + 4])  # type: ignore[return-value]

    def lit(x: int, y: int) -> bool:
        red, green, blue, alpha = pixel(x, y)
        channel_range = max(red, green, blue) - min(red, green, blue)
        # The current Buddy palette is intentionally pale/neutral like the
        # DAGOAL reference, so chroma alone cannot distinguish foreground
        # from the dark field. Keep the chromatic threshold for colored
        # telemetry while admitting bright neutral phosphor cells.
        return alpha > 180 and max(red, green, blue) > 90 and (
            channel_range > 18 or max(red, green, blue) > 150
        )

    lit_count = sum(lit(x, y) for y in range(HEIGHT) for x in range(WIDTH))
    if lit_count < 120:
        fail(f"OI phosphor field is nearly empty ({lit_count} lit pixels)")

    telemetry_lit = sum(lit(x, y) for y in range(40, 165) for x in range(12, 218))
    if telemetry_lit < 80:
        fail(f"active telemetry lane is nearly empty ({telemetry_lit} lit pixels)")

    # Buddy is a recognizable actor in the world lane, not the dominant
    # foreground. Bound its occupancy as well as requiring a real silhouette.
    actor_rows = {
        y
        for y in range(148, 244)
        if sum(lit(x, y) for x in range(230, 350)) >= 2
    }
    actor_columns = {
        x
        for x in range(230, 350)
        if sum(lit(x, y) for y in range(148, 244)) >= 2
    }
    if len(actor_rows) < 18 or len(actor_columns) < 18:
        fail(
            "world actor does not have a recognizable bounded matrix footprint "
            f"(rows={len(actor_rows)}, columns={len(actor_columns)})"
        )
    actor_lit = sum(lit(x, y) for y in range(148, 244) for x in range(230, 350))
    if actor_lit > 2200:
        fail(f"world actor occupies too much of the CRT ({actor_lit} lit pixels)")

    # No post-process scanline may dominate a row. Foreground remains visible
    # because the CRT modulation is required to be subordinate/background-only.
    def horizontal_run_bounds(y: int) -> tuple[int, int, int]:
        longest = current = 0
        longest_start = longest_end = 0
        current_start = 0
        for x in range(WIDTH):
            if lit(x, y):
                if current == 0:
                    current_start = x
                current += 1
                if current > longest:
                    longest = current
                    longest_start = current_start
                    longest_end = x
            else:
                current = 0
        return longest, longest_start, longest_end

    row_bounds = [horizontal_run_bounds(y) for y in range(HEIGHT)]
    long_rows = [
        (y, start, end, length)
        for y, (length, start, end) in enumerate(row_bounds)
        if length > WIDTH * 0.72
    ]

    def is_authored_panel_border() -> bool:
        """Recognize the two-sided readout frame, not an overlay scanline.

        The OI contract intentionally rejects a full-width post-process line.
        Read/code/test scenes also have an authored readout pane whose top and
        bottom rules are long by design.  It is only exempted when there are
        exactly two aligned long rows and both rows have matching vertical
        side support, which a scanline cannot provide.
        """
        if len(long_rows) != 2:
            return False
        (top, top_start, top_end, _), (bottom, bottom_start, bottom_end, _) = long_rows
        if bottom - top < 32:
            return False
        if abs(top_start - bottom_start) > 12 or abs(top_end - bottom_end) > 12:
            return False
        left_band = range(max(0, min(top_start, bottom_start) - 8), min(WIDTH, max(top_start, bottom_start) + 4))
        right_band = range(max(0, min(top_end, bottom_end) - 4), min(WIDTH, max(top_end, bottom_end) + 9))
        middle_rows = range(top + 3, bottom - 2)
        left_support = sum(any(lit(x, y) for x in left_band) for y in middle_rows)
        right_support = sum(any(lit(x, y) for x in right_band) for y in middle_rows)
        span = max(1, bottom - top - 5)
        return left_support >= span * 0.45 and right_support >= span * 0.45

    max_row = max(range(HEIGHT), key=lambda y: row_bounds[y][0])
    if row_bounds[max_row][0] > WIDTH * 0.72 and not is_authored_panel_border():
        fail(
            "a scanline-like full-width foreground overlay dominates the scene "
            f"(row={max_row}, contiguous_lit={row_bounds[max_row][0]})"
        )
    dense_rows = sum(length > WIDTH * 0.55 for length, _, _ in row_bounds)
    if dense_rows > 18:
        fail(f"CRT treatment is too dense ({dense_rows} high-occupancy rows)")

    print(
        "native-oi-visual: PASS — "
        f"{lit_count} phosphor pixels, telemetry {telemetry_lit}, actor {actor_lit}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
