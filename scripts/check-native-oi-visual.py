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
        return alpha > 180 and max(red, green, blue) > 90 and channel_range > 18

    lit_count = sum(lit(x, y) for y in range(HEIGHT) for x in range(WIDTH))
    if lit_count < 120:
        fail(f"OI phosphor field is nearly empty ({lit_count} lit pixels)")

    # Buddy is staged in the middle/lower world. Count occupied row and column
    # bands there; a former 14x11 ASCII sprite cannot satisfy these floors.
    actor_rows = {
        y
        for y in range(132, 244)
        if sum(lit(x, y) for x in range(92, 286)) >= 5
    }
    actor_columns = {
        x
        for x in range(88, 290)
        if sum(lit(x, y) for y in range(132, 244)) >= 5
    }
    if len(actor_rows) < 18 or len(actor_columns) < 18:
        fail(
            "lower-world actor does not have a 28x32-class dot-matrix footprint "
            f"(rows={len(actor_rows)}, columns={len(actor_columns)})"
        )

    # No post-process scanline may dominate a row. Foreground remains visible
    # because the CRT modulation is required to be subordinate/background-only.
    def horizontal_run(y: int) -> int:
        longest = current = 0
        for x in range(WIDTH):
            current = current + 1 if lit(x, y) else 0
            longest = max(longest, current)
        return longest

    row_runs = [horizontal_run(y) for y in range(HEIGHT)]
    max_row = max(range(HEIGHT), key=row_runs.__getitem__)
    if row_runs[max_row] > WIDTH * 0.72:
        fail(
            "a scanline-like full-width foreground overlay dominates the scene "
            f"(row={max_row}, contiguous_lit={row_runs[max_row]})"
        )

    print(
        "native-oi-visual: PASS — "
        f"{lit_count} phosphor pixels, actor bands {len(actor_rows)}x{len(actor_columns)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
