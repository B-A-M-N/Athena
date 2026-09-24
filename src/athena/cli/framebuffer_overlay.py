"""Buddy overlay encoding for the Pillow OI framebuffer.

The static scene and animation layers remain owned by ``OIFrameBuffer``. This
mechanism only turns the already-positioned Buddy world into a clipped PNG
framebuffer envelope for Glass/Kitty presentation.
"""

from __future__ import annotations

import io
from typing import Any

from athena.cli.animation import OIVisualState
from athena.presentation.scene import OIScene

__all__ = ["BuddyOverlayRenderer"]


class BuddyOverlayRenderer:
    """Encode the transparent Buddy layer without owning scene or cache state."""

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    def render(self, scene: OIScene, visual: OIVisualState, width: int, height: int) -> Any:
        if self._owner.Image is None:
            return None
        width, height = max(int(width), 80), max(int(height), 60)
        key = (width, height, self._owner._scene_key(scene))
        if visual.semantic_state == "hidden":
            return self._owner.FrameBuffer(b"", width, height, layer="overlay", base_key=key)
        position = self._owner._buddy_position(scene, visual, width, height)
        if position is None:
            return self._owner.FrameBuffer(b"", width, height, layer="overlay", base_key=key)
        left, top = position
        world = self._owner._world.render(visual, status=scene.status)
        encoded = io.BytesIO()
        world.save(encoded, format="PNG", optimize=False, compress_level=1)
        return self._owner.FrameBuffer(
            encoded.getvalue(),
            width,
            height,
            dirty_region=(left, top, self._owner.BUDDY_WORLD_WIDTH, self._owner.BUDDY_WORLD_HEIGHT),
            layer="overlay",
            base_key=key,
        )
