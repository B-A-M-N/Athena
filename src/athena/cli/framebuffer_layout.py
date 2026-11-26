"""Collision-safe Buddy placement for the retained OI framebuffer."""

from __future__ import annotations

from athena.cli.animation import OIVisualState
from athena.presentation.scene import OIScene, tree_rows
from athena.presentation.semantics import VisualActionKind

__all__ = ["BuddyPlacement"]


class BuddyPlacement:
    """Compute deterministic mascot placement around scene content."""

    WORLD_WIDTH = 176
    WORLD_HEIGHT = 136

    @classmethod
    def position(
        cls, scene: OIScene, visual: OIVisualState, width: int, height: int
    ) -> tuple[int, int] | None:
        start_fx, start_fy = scene.anchors.get(visual.previous_anchor, scene.anchors["center"])
        end_fx, end_fy = scene.anchors.get(scene.buddy_anchor, scene.anchors["center"])
        progress = min(max(visual.transition, 0.0), 1.0)
        eased = progress * progress * (3.0 - 2.0 * progress)
        fx = start_fx + (end_fx - start_fx) * eased
        fy = start_fy + (end_fy - start_fy) * eased
        desired = (
            int(width * fx),
            int(height * fy) + (0 if progress >= 1 else int((1 - progress) * 10)),
        )
        for center_x, center_y in cls._candidates(desired):
            left = center_x - cls.WORLD_WIDTH // 2
            top = center_y - cls.WORLD_HEIGHT // 2
            if (
                left < 0
                or top < 0
                or left + cls.WORLD_WIDTH > width
                or top + cls.WORLD_HEIGHT > height
            ):
                continue
            if not cls.overlaps_content(scene, left, top, width, height):
                return center_x, center_y
        return None

    @classmethod
    def _candidates(cls, desired: tuple[int, int]) -> tuple[tuple[int, int], ...]:
        desired_left = round((desired[0] - cls.WORLD_WIDTH // 2) / 10) * 10
        desired_top = round((desired[1] - cls.WORLD_HEIGHT // 2) / 20) * 20
        candidates: list[tuple[int, int, int, int]] = []
        for radius in range(21):
            for dy, dx in (
                (0, 0),
                (-radius * 20, 0),
                (radius * 20, 0),
                (0, -radius * 10),
                (0, radius * 10),
                (-radius * 20, -radius * 10),
                (-radius * 20, radius * 10),
                (radius * 20, -radius * 10),
                (radius * 20, radius * 10),
            ):
                left, top = desired_left + dx, desired_top + dy
                candidates.append((abs(left - desired_left) + abs(top - desired_top), top, left, 0))
        ordered: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        for _distance, top, left, _ in sorted(candidates):
            center = (left + cls.WORLD_WIDTH // 2, top + cls.WORLD_HEIGHT // 2)
            if center not in seen:
                seen.add(center)
                ordered.append(center)
        return tuple(ordered)

    @classmethod
    def overlaps_content(cls, scene: OIScene, left: int, top: int, width: int, height: int) -> bool:
        right, bottom = left + cls.WORLD_WIDTH, top + cls.WORLD_HEIGHT
        return any(
            left < region_right
            and right > region_left
            and top < region_bottom
            and bottom > region_top
            for region_left, region_top, region_right, region_bottom in cls.content_regions(
                scene, width, height
            )
        )

    @staticmethod
    def content_regions(
        scene: OIScene, width: int, height: int
    ) -> tuple[tuple[int, int, int, int], ...]:
        """Approximate occupied regions used for collision-safe placement."""
        margin = max(18, width // 24)
        top = max(14, height // 22)
        regions: list[tuple[int, int, int, int]] = [
            (margin, top - 2, width - margin, top + 63),
            (margin, height - 103, width - margin, height - 34),
        ]
        body_top = top + 78
        if scene.mode is VisualActionKind.IDLE:
            middle = width // 2
            regions.append((middle - 2, body_top - 4, middle + 3, height - 52))
            regions.extend(
                (margin, body_top - 2 + index * 20, middle - 8, body_top + 16 + index * 20)
                for index, _ in enumerate(tree_rows(scene.workspace_tree)[:8])
            )
            regions.extend(
                (middle + 8, body_top - 2 + index * 20, width - margin, body_top + 16 + index * 20)
                for index, _ in enumerate(tree_rows(scene.runtime_tree)[:8])
            )
        else:
            regions.append((margin, body_top - 2, width - margin, height - 106))
        return tuple(regions)
