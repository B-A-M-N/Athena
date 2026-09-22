"""Retained Glass-layer presentation for the dual-pane CLI surface.

This module owns only the optional pixel transport: framebuffer layer
composition, Kitty image identities, dirty-region placement, and retained base
identity.  The dual-pane surface remains responsible for terminal lifecycle,
layout, and projection state.
"""

from __future__ import annotations

from typing import Any, TextIO

from athena.cli.animation import OIVisualState
from athena.cli.framebuffer import OIFrameBuffer
from athena.cli.render.kitty import KittyAsset, KittyGraphicsProtocol
from athena.presentation.layout import AthenaLayout
from athena.presentation.scene import OIScene


class GlassPresentation:
    """Present retained OI layers into a Kitty-capable terminal viewport."""

    _FRAME_ID = 40
    _OVERLAY_ID = 41
    _MOTION_ID = 42

    def __init__(
        self,
        output: TextIO,
        framebuffer: OIFrameBuffer,
        kitty: KittyGraphicsProtocol,
    ) -> None:
        self._output = output
        self._framebuffer = framebuffer
        self._kitty = kitty
        self._base_key: tuple[Any, ...] | None = None

    def cleanup(self) -> str:
        """Release all image identities owned by this presentation."""
        return self._kitty.cleanup()

    def present(self, *, layout: AthenaLayout, scene: OIScene, visual: OIVisualState) -> None:
        """Render and place the retained base, motion, and overlay layers."""
        viewport = layout.oi
        pixel_width = max(viewport.width * 10, 80)
        pixel_height = max((viewport.height - 2) * 20, 60)
        base = self._framebuffer.render_base(scene, pixel_width, pixel_height)
        motion = self._framebuffer.render_motion_overlay(scene, visual, pixel_width, pixel_height)
        overlay = self._framebuffer.render_overlay(scene, visual, pixel_width, pixel_height)
        if base is None or motion is None or overlay is None:
            return

        command = ""
        if base.base_key != self._base_key:
            command += self._kitty.present(
                KittyAsset(self._FRAME_ID, base.png),
                x=viewport.x + 1,
                y=viewport.y + 1,
                columns=max(viewport.width - 2, 1),
                rows=max(viewport.height - 2, 1),
            )
            self._base_key = base.base_key

        if motion.png and motion.dirty_region:
            left, top, region_width, region_height = motion.dirty_region
            command += self._kitty.present(
                KittyAsset(self._MOTION_ID, motion.png),
                x=viewport.x + 1 + left // 10,
                y=viewport.y + 1 + top // 20,
                columns=max((region_width + 9) // 10, 1),
                rows=max((region_height + 19) // 20, 1),
            )
        else:
            command += self._kitty.delete(self._MOTION_ID)

        if overlay.png and overlay.dirty_region:
            left, top, region_width, region_height = overlay.dirty_region
            command += self._kitty.present(
                KittyAsset(self._OVERLAY_ID, overlay.png),
                x=viewport.x + 1 + left // 10,
                y=viewport.y + 1 + top // 20,
                columns=max((region_width + 9) // 10, 1),
                rows=max((region_height + 19) // 20, 1),
            )
        else:
            command += self._kitty.delete(self._OVERLAY_ID)

        if command:
            # Kitty placements with C=1 leave the cursor at the placement
            # origin. Restore it to the integrated prompt before input.
            command += f"\x1b[{layout.prompt.y + 2};1H"
            self._output.write(command)
            self._output.flush()


__all__ = ["GlassPresentation"]
