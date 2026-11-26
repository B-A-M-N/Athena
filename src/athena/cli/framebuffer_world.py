"""Bounded animated Buddy-world pixel rendering for the Glass framebuffer."""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any

from athena.cli.animation import OIVisualState
from athena.cli.render.dotmatrix import build_owl_field, hash2

__all__ = ["BuddyWorld"]


class BuddyWorld:
    """Render the retained, transparent mascot terrain layer."""

    WIDTH = 176
    HEIGHT = 136
    OWL_WIDTH = 108
    OWL_HEIGHT = 100

    def __init__(self, image: Any, image_draw: Any, image_filter: Any) -> None:
        self._image = image
        self._image_draw = image_draw
        self._image_filter = image_filter

    @staticmethod
    @lru_cache(maxsize=4)
    def _owl_field(image: Any, image_draw: Any):
        if image is None:
            return None
        return build_owl_field(image, image_draw)

    def render(self, visual: OIVisualState, *, status: str) -> Any:
        """Return one bounded transparent dot-matrix world."""
        image = self._image.new(
            "RGBA",
            (self.WIDTH, self.HEIGHT),
            (0, 0, 0, 0),
        )
        self._draw_dot_grid(image, visual)
        self._render_dot_owl(image, visual, status=status)
        return image

    def _draw_dot_grid(self, image: Any, visual: OIVisualState) -> None:
        """Render the bounded terrain as discrete point-sampled mesh cells."""
        draw = self._image_draw.Draw(image, "RGBA")
        time_s = visual.ambient_time + visual.grid_phase * 20.0
        depth_count, lateral_count = 15, 16

        for depth_index in range(depth_count):
            depth = depth_index / (depth_count - 1)
            for u_index in range(lateral_count + 1):
                u = u_index / lateral_count * 2.0 - 1.0
                x, y, p = self._project_grid(u, depth, time_s)
                if self._grid_gate(u, depth, 411) < 0.20:
                    continue
                alpha = int(24 + 66 * p)
                color = (104, 165, 224, alpha)
                size = 1.0 + p * 1.25
                draw.rectangle(self._dot_rect(x, y, size), fill=color)

        for lateral_index in range(1, lateral_count):
            u = lateral_index / lateral_count * 2.0 - 1.0
            for depth_index in range(1, depth_count):
                depth = depth_index / (depth_count - 1)
                if lateral_index % 4 == 0 or depth_index % 3 == 0:
                    continue
                x, y, p = self._project_grid(u, depth, time_s)
                if self._grid_gate(u, depth, 517) < 0.30:
                    continue
                alpha = int(18 + 42 * p)
                color = (84, 137, 199, alpha)
                draw.rectangle(self._dot_rect(x, y, 1.0 + p), fill=color)

    @staticmethod
    def _grid_gate(u: float, depth: float, seed: int) -> float:
        gx = int(round((u + 1.0) * 1000.0))
        gy = int(round(depth * 1000.0))
        return hash2(gx, gy, seed)

    @staticmethod
    def _dot_rect(x: float, y: float, size: float) -> tuple[int, int, int, int]:
        px, py, cell = round(x), round(y), max(1, round(size))
        return (px, py, px + cell - 1, py + cell - 1)

    @classmethod
    def _project_grid(cls, u: float, depth: float, time_s: float) -> tuple[float, float, float]:
        horizon_y = cls.HEIGHT * 0.60
        bottom_y = cls.HEIGHT * 1.04
        center_x = cls.WIDTH * 0.50

        p = depth**1.38
        half_width = cls.WIDTH * (0.18 + p * 0.54)
        x = center_x + u * half_width

        wave_a = math.sin(u * 5.6 + depth * 7.0 + time_s * 0.32)
        wave_b = math.sin(u * 2.1 - depth * 10.5 - time_s * 0.20)
        wave = (wave_a * 0.65 + wave_b * 0.35) * 3.0 * (0.25 + p * 0.75)
        y = horizon_y + p * (bottom_y - horizon_y) + wave
        return x, y, p

    def _render_dot_owl(
        self,
        image: Any,
        visual: OIVisualState,
        *,
        status: str,
    ) -> None:
        """Paint the stable sampled owl field with mild phosphor shimmer."""
        field = self._owl_field(self._image, self._image_draw)
        if field is None or not any((field.outline, field.wings, field.body, field.face)):
            return

        status = str(status).upper()
        phosphor = (164, 214, 255, 238)
        warn = (222, 176, 108, 245)
        bad = (224, 119, 126, 247)
        face_color = (
            bad if status in {"FAILURE", "BLOCKED"} else warn if status == "APPROVAL" else phosphor
        )
        draw = self._image_draw.Draw(image, "RGBA")

        glow = self._image.new("RGBA", image.size, (0, 0, 0, 0))
        glow_draw = self._image_draw.Draw(glow, "RGBA")
        epoch = int(visual.ambient_time * 5.0)
        offset_x = (self.WIDTH - self.OWL_WIDTH) // 2
        offset_y = (self.HEIGHT - self.OWL_HEIGHT) // 2 + 3
        layer_seeds = {"body": 71, "wings": 73, "outline": 79, "face": 83}

        for layer_name, cells in (
            ("body", field.body),
            ("wings", field.wings),
            ("outline", field.outline),
            ("face", field.face),
        ):
            layer_seed = layer_seeds[layer_name]
            for point_index, point in enumerate(cells):
                dropout = hash2(point_index, epoch, 1201)
                if dropout < 0.035 * (0.55 + point.gate * 0.9):
                    continue

                shimmer = 0.90 + 0.10 * math.sin(
                    visual.ambient_time * math.tau * 0.85 + point.phase
                )
                x = offset_x + 6 + point.x * (self.OWL_WIDTH - 12)
                y = offset_y + 5 + point.y * (self.OWL_HEIGHT - 10)
                y += math.sin(visual.ambient_time * math.tau * 0.18) * 2.2

                if layer_name == "face":
                    color, alpha, size = face_color, 250, point.size + 0.45
                elif layer_name == "outline":
                    color = face_color if hash2(point_index, 7, layer_seed) < 0.07 else phosphor
                    alpha, size = 235, point.size
                elif layer_name == "wings":
                    color, alpha, size = phosphor, 190, point.size - 0.1
                else:
                    color, alpha, size = phosphor, 115, point.size - 0.2

                fill = (color[0], color[1], color[2], max(8, int(alpha * shimmer)))
                rect = self._dot_rect(x, y, max(1.0, size))
                draw.rectangle(rect, fill=fill)
                glow_draw.rectangle(rect, fill=(fill[0], fill[1], fill[2], fill[3] // 3))

        image.alpha_composite(self._image_filter.GaussianBlur(glow, radius=1.25))
