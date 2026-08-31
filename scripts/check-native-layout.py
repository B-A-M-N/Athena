#!/usr/bin/env python3
"""Validate one native layout dump against the AthenaBOX resize contract."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path


DESIGN_WIDTH = 1672.0
DESIGN_HEIGHT = 941.0


def fail(message: str) -> None:
    raise AssertionError(message)


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(f"usage: {argv[0]} LAYOUT.json WIDTH HEIGHT", file=sys.stderr)
        return 2
    layout = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    width = int(argv[2])
    height = int(argv[3])

    if layout.get("width") != width or layout.get("height") != height:
        fail("layout dimensions do not match the requested surface")
    if layout.get("metrics_source") not in {"live_xft", "fallback_static"}:
        fail("layout metrics source is not explicitly classified")
    scale = float(layout["scale"])
    expected_scale = min(width / DESIGN_WIDTH, height / DESIGN_HEIGHT)
    if not math.isclose(scale, expected_scale, rel_tol=0.0, abs_tol=0.001):
        fail(f"unexpected uniform scale: {scale} != {expected_scale}")

    canvas = layout["canvas"]
    if not math.isclose(canvas["width"] / canvas["height"], DESIGN_WIDTH / DESIGN_HEIGHT, abs_tol=0.001):
        fail("canvas aspect ratio changed")
    if canvas["x"] < -0.01 or canvas["y"] < -0.01:
        fail("canvas begins outside the surface")
    if canvas["x"] + canvas["width"] > width + 0.01 or canvas["y"] + canvas["height"] > height + 0.01:
        fail("canvas extends outside the surface")

    for name in (
        "header",
        "operator_outer",
        "oi_outer",
        "operator_inner",
        "operator_viewport",
        "oi_inner",
        "controls",
        "prompt",
    ):
        rect = layout[name]
        if rect["x"] < -0.01 or rect["y"] < -0.01:
            fail(f"{name} begins outside the surface")
        if rect["x"] + rect["width"] > width + 0.01 or rect["y"] + rect["height"] > height + 0.01:
            fail(f"{name} extends outside the surface")

    if not math.isclose(layout["operator_outer"]["width"], layout["oi_outer"]["width"], abs_tol=0.01):
        fail("operator and OI wells are not equal-width apertures")
    if not math.isclose(layout["operator_inner"]["height"], layout["oi_inner"]["height"], abs_tol=0.01):
        fail("operator and OI inner apertures are not equal-height")
    if layout["operator_outer"]["height"] < canvas["height"] * 0.60:
        fail("display assembly is no longer dominant")
    if layout["controls"]["height"] > canvas["height"] * 0.18:
        fail("control rail is no longer shallow")

    prompt = layout["prompt_layout"]
    prompt_rect = layout["prompt"]
    if prompt["rect"] != prompt_rect:
        fail("prompt layout and prompt rectangle diverged")
    previous_bottom = prompt_rect["y"]
    for name in ("status_row", "input_row", "hint_row"):
        row = prompt.get(name)
        if row is None:
            continue
        if row["top"] < previous_bottom - 0.01:
            fail(f"prompt rows overlap: {name}")
        if row["top"] + row["height"] > prompt_rect["y"] + prompt_rect["height"] + 0.01:
            fail(f"prompt row escapes prompt bay: {name}")
        previous_bottom = row["top"] + row["height"]

    rail = layout["rail"]
    rail_rect = rail["rail"]
    modules = (
        "speaker",
        "operator_panel",
        "operator_status",
        "operator_input",
        "operator_hint",
        "system_status",
        "primary_encoder",
        "brightness",
        "focus",
        "power",
        "identity_plate",
    )
    for name in modules:
        rect = rail.get(name)
        if rect is None:
            continue
        if rect["x"] < rail_rect["x"] - 0.01 or rect["y"] < rail_rect["y"] - 0.01:
            fail(f"{name} escapes the control rail")
        if rect["x"] + rect["width"] > rail_rect["x"] + rail_rect["width"] + 0.01:
            fail(f"{name} escapes the control rail")
        if rect["y"] + rect["height"] > rail_rect["y"] + rail_rect["height"] + 0.01:
            fail(f"{name} escapes the control rail")
    if rail["identity_plate"]["x"] <= rail["power"]["x"]:
        fail("identity plate is not the far-right hardware module")

    metrics = layout["metrics"]
    if any(float(metrics[name]["height"]) <= 0 or float(metrics[name]["width"]) <= 0 for name in metrics):
        fail("font metrics contain a non-positive dimension")
    terminal = layout.get("terminal_size")
    if terminal is not None and (terminal["columns"] < 1 or terminal["rows"] < 1):
        fail("PTY dimensions are not positive")

    sizes = layout.get("font_pixel_sizes")
    if layout["metrics_source"] == "live_xft":
        if sizes is None or len(sizes) != 4:
            fail("live layout is missing quantized font sizes")
        expected_sizes = [
            max(10, round(14 * scale)),
            max(10, round(15 * scale)),
            max(9, round(12 * scale)),
            max(7, round(9 * scale)),
        ]
        if sizes != expected_sizes:
            fail(f"font sizes do not follow cabinet scale: {sizes} != {expected_sizes}")

    print(f"native-layout: PASS — {width}x{height}, scale={scale:.5f}, source={layout['metrics_source']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except (AssertionError, KeyError, json.JSONDecodeError, OSError, ValueError) as error:
        print(f"native-layout: FAIL — {error}", file=sys.stderr)
        raise SystemExit(1) from error
