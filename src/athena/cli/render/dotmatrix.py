"""Deterministic mask sampling for Pillow dot-matrix rendering."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class DotCell:
    """One sampled cell, normalized to its source mask."""

    x: float
    y: float
    size: float
    phase: float
    bias: float
    gate: float


@dataclass(frozen=True, slots=True)
class DotField:
    """Sampled semantic layers of the Athena owl mask."""

    outline: tuple[DotCell, ...]
    wings: tuple[DotCell, ...]
    body: tuple[DotCell, ...]
    face: tuple[DotCell, ...]


def hash2(x: int, y: int, seed: int = 0) -> float:
    """Return a stable unit value without introducing animation-time RNG."""
    n = (x * 374_761_393 + y * 668_265_263 + seed * 1_442_695_041) & 0xFFFFFFFF
    n = ((n ^ (n >> 13)) * 1_274_126_177) & 0xFFFFFFFF
    n ^= n >> 16
    return n / 0xFFFFFFFF


def sample_mask(
    image: Any,
    *,
    step: int,
    threshold: int,
    seed: int,
    keep: float = 1.0,
) -> tuple[DotCell, ...]:
    """Sample an offscreen mask into stable, normalized phosphor points."""
    px = image.load()
    width, height = image.size
    points: list[DotCell] = []

    for y in range(0, height, step):
        for x in range(0, width, step):
            value = px[x, y]
            if value < threshold:
                continue

            gate = hash2(x, y, seed)
            if gate > keep:
                continue

            points.append(
                DotCell(
                    x=x / width,
                    y=y / height,
                    size=1.25 + hash2(x + 91, y + 7, seed) * 1.0,
                    phase=hash2(x + 17, y + 31, seed) * math.tau,
                    bias=0.88 + hash2(x + 5, y + 131, seed) * 0.12,
                    gate=hash2(x, y, seed + 99),
                )
            )

    return tuple(points)


def build_owl_field(image_cls: Any, draw_cls: Any) -> DotField:
    """Rasterize an owl to throwaway masks and sample its phosphor field.

    The masks themselves are never composited.  Their purpose is to establish
    shape; only the resulting square cells are visible in the Glass world.
    """
    size = (144, 120)

    def mask() -> Any:
        return image_cls.new("L", size, 0)

    outline = mask()
    wings = mask()
    body = mask()
    face = mask()

    # Head perimeter and angular ear tufts establish the owl silhouette.
    # The construction mask is raster-only and is deliberately never composited.
    draw = draw_cls.Draw(outline)
    draw.rounded_rectangle((28, 20, 116, 82), radius=28, outline=255, width=5)
    draw.line(((34, 35), (21, 9), (49, 25)), fill=255, width=4, joint="curve")
    draw.line(((110, 35), (123, 9), (95, 25)), fill=255, width=4, joint="curve")

    # Prominent rings carry the owl identity at low dot counts.
    draw.ellipse((40, 34, 68, 62), outline=255, width=5)
    draw.ellipse((76, 34, 104, 62), outline=255, width=5)

    # Keep the torso open and the wings comparatively dense. The masks are
    # construction geometry only; the public DotField contains sampled points.
    draw = draw_cls.Draw(body)
    draw.ellipse((47, 69, 97, 118), fill=255)
    draw = draw_cls.Draw(wings)
    draw.ellipse((27, 72, 59, 111), fill=255)
    draw.ellipse((85, 72, 117, 111), fill=255)

    # Pupils and beak form the small high-contrast semantic signal layer.
    draw = draw_cls.Draw(face)
    draw.ellipse((50, 44, 58, 54), fill=255)
    draw.ellipse((86, 44, 94, 54), fill=255)
    draw.polygon(((67, 63), (72, 73), (77, 63)), fill=255)

    return DotField(
        outline=sample_mask(outline, step=4, threshold=64, seed=11, keep=0.94),
        wings=sample_mask(wings, step=4, threshold=72, seed=23, keep=0.78),
        body=sample_mask(body, step=5, threshold=72, seed=37, keep=0.42),
        face=sample_mask(face, step=3, threshold=64, seed=53, keep=1.0),
    )


__all__ = ["DotCell", "DotField", "build_owl_field", "hash2", "sample_mask"]
