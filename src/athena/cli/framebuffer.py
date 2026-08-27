"""Pillow-backed OI framebuffer for the Glass CRT viewport.

The framebuffer is intentionally scene-only: it receives a fixed viewport and
cannot affect chassis geometry.  If Pillow is not installed, callers fall back
to the ANSI OI scene automatically.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

from athena.cli.animation import OIVisualState
from athena.cli.scene import OIScene

try:  # Optional so plain/ANSI installs remain lightweight.
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:  # pragma: no cover - exercised in minimal installs
    Image = ImageDraw = ImageFilter = ImageFont = None


Color: TypeAlias = tuple[int, int, int] | tuple[int, int, int, int]


def _state_marker(status: object) -> str:
    return {
        "complete": "✓",
        "success": "✓",
        "failed": "!",
        "failure": "!",
        "blocked": "!",
        "approval": "?",
        "running": "●",
    }.get(str(status).lower(), "·")


def pillow_available() -> bool:
    return Image is not None


@dataclass(frozen=True)
class FrameBuffer:
    png: bytes
    width: int
    height: int


class OIFrameBuffer:
    """Render a restrained blue-black computational world as PNG."""

    _FONT_PATHS = (
        "/usr/share/fonts/opentype/fira/FiraMono-Regular.otf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
    )

    def __init__(self, *, font_path: str | None = None) -> None:
        self.font_path = font_path
        self._fonts: dict[int, Any] = {}
        self._base_frames: dict[tuple[int, int, tuple[Any, ...]], Any] = {}

    def _font(self, size: int):
        if ImageFont is None:
            return None
        size = max(int(size), 8)
        if size in self._fonts:
            return self._fonts[size]
        paths = ([self.font_path] if self.font_path else []) + list(self._FONT_PATHS)
        for candidate in paths:
            if candidate and Path(candidate).is_file():
                try:
                    font = ImageFont.truetype(candidate, size)
                    self._fonts[size] = font
                    return font
                except OSError:
                    pass
        font = ImageFont.load_default()
        self._fonts[size] = font
        return font

    @staticmethod
    def _scene_key(scene: OIScene) -> tuple[Any, ...]:
        """Return the stable content identity for the cached static layer."""
        entities = tuple(
            (
                entity.id,
                entity.kind,
                entity.label,
                entity.status,
                entity.anchor,
                tuple(sorted((str(key), repr(value)) for key, value in entity.metadata.items())),
            )
            for entity in scene.entities
        )
        return (
            scene.title,
            scene.status,
            entities,
            tuple(scene.alerts),
            tuple(scene.stream),
        )

    @staticmethod
    def _entity_color(entity: Any, ink: Color, accent: Color, warn: Color, bad: Color) -> Color:
        state = str(getattr(entity, "status", "")).lower()
        if state in {"failed", "failure", "blocked", "error"}:
            return bad
        if state in {"approval", "waiting", "warning"}:
            return warn
        if state in {"active", "running", "requested", "validated"}:
            return accent
        return ink

    @staticmethod
    def _text(draw: Any, xy: tuple[int, int], text: object, font: Any, fill: Color, *, spacing: int = 2) -> None:
        draw.text(xy, str(text), font=font, fill=fill, spacing=spacing)

    def _render_base(self, scene: OIScene, width: int, height: int) -> Any:
        """Render everything that does not change during an animation tick."""
        image = Image.new("RGBA", (width, height), (9, 15, 31, 255))
        draw = ImageDraw.Draw(image, "RGBA")

        # Glass depth: vignette-like bands, a faint perspective grid, and
        # scanlines. These are static atmosphere, not semantic state.
        for y in range(height):
            mix = y / max(height - 1, 1)
            draw.line((0, y, width, y), fill=(10 + int(4 * mix), 17 + int(9 * mix), 35 + int(15 * mix), 255))
        for x in range(-height, width + height, max(42, width // 14)):
            draw.line((width // 2, height // 2, x, height), fill=(37, 56, 94, 34), width=1)
        for y in range(height // 2, height, max(28, height // 9)):
            draw.line((0, y, width, y), fill=(47, 72, 111, 28), width=1)
        for y in range(2, height, 4):
            draw.line((0, y, width, y), fill=(139, 177, 219, 8), width=1)

        margin = max(18, width // 24)
        top = max(14, height // 22)
        ink = (177, 196, 225, 228)
        dim = (112, 140, 180, 180)
        bright = (218, 231, 249, 245)
        accent = (101, 183, 206, 220)
        warn = (222, 176, 108, 235)
        bad = (224, 119, 126, 235)
        font_small = self._font(max(10, width // 64))
        font = self._font(max(12, width // 48))

        self._text(draw, (margin, top), scene.title, font_small, bright)
        self._text(draw, (margin, top + 22), f"MODE  {scene.status:<14}  VIEWPORT  {width}×{height}", font_small, dim)
        draw.line((margin, top + 43, width - margin, top + 43), fill=(115, 154, 196, 95), width=1)

        left = margin
        right = width // 2 + 4
        body_top = top + 62
        self._text(draw, (left, body_top), "WORKSPACE MAP", font_small, dim)
        self._text(draw, (right, body_top), "RUNTIME GRAPH", font_small, dim)
        draw.line((width // 2, body_top - 4, width // 2, height - 52), fill=(92, 125, 165, 50), width=1)

        resources = [
            entity for entity in scene.entities
            if entity.kind in {"resource", "research", "artifact"}
        ]
        tree = [
            (
                f"{('✓' if entity.status in {'complete', 'success'} else '!' if entity.status in {'failed', 'failure'} else '·')} {entity.label}",
                self._entity_color(entity, ink, accent, warn, bad),
            )
            for entity in resources[:8]
        ] or [("· no workspace resources observed", dim)]
        for idx, (label, color) in enumerate(tree):
            self._text(draw, (left, body_top + 24 + idx * 20), label, font, color)

        runtime_entities = [
            entity for entity in scene.entities
            if entity.kind in {"operation", "child_task", "workflow", "verification", "generated_tool"}
        ]
        gx = right + (width - right) // 2
        if not runtime_entities:
            self._text(draw, (right, body_top + 42), "· no runtime operations observed", font_small, dim)
        nodes = []
        for idx, entity in enumerate(runtime_entities[:6]):
            x = gx + ((-1 if idx % 2 else 1) * min(48, max(24, (width - right) // 6)))
            y = body_top + 42 + (idx // 2) * 58
            nodes.append((entity, x, y))
        for entity, x, y in nodes:
            parent_id = str(entity.metadata.get("parent_id") or "")
            parent = next((item for item in nodes if item[0].id == parent_id), None)
            if parent:
                _, px, py = parent
                draw.line((px, py + 12, x, y - 12), fill=(88, 145, 183, 150), width=1)
        for entity, x, y in nodes:
            color = self._entity_color(entity, ink, accent, warn, bad)
            label = f"{entity.label} {_state_marker(entity.status)}"
            draw.rounded_rectangle((x - 54, y - 12, x + 54, y + 12), radius=4, outline=color, fill=(20, 35, 61, 175), width=1)
            self._text(draw, (x - 45, y - 7), label[:18], font_small, color)

        # Stream/alert band stays subordinate to the scene. It still exposes
        # live data, but the graphical OI is not just a firehose.
        band_y = height - 102
        draw.line((margin, band_y, width - margin, band_y), fill=(115, 154, 196, 75), width=1)
        self._text(draw, (margin, band_y + 10), "LIVE TRACE", font_small, dim)
        entries = scene.alerts[-2:] or scene.stream[-2:] or ["awaiting canonical events"]
        for idx, entry in enumerate(entries):
            color = bad if "fail" in entry.lower() or "error" in entry.lower() else warn if "approval" in entry.lower() else ink
            self._text(draw, (margin, band_y + 30 + idx * 18), entry[: max(20, width // 12)], font_small, color)

        # Very faint corner glass highlights make the CRT read as glass without
        # obscuring text or pretending to be a full-screen screenshot.
        draw.arc((width - 110, -45, width + 48, 76), 168, 286, fill=(200, 224, 255, 20), width=2)
        draw.rectangle((2, 2, width - 3, height - 3), outline=(125, 157, 198, 68), width=1)
        return image

    def render(self, scene: OIScene, visual: OIVisualState, width: int, height: int) -> FrameBuffer | None:
        if Image is None:
            return None
        width, height = max(int(width), 80), max(int(height), 60)
        key = (width, height, self._scene_key(scene))
        base = self._base_frames.get(key)
        if base is None:
            base = self._render_base(scene, width, height)
            self._base_frames[key] = base
            # Live traces can produce many distinct scene keys. Retain only a
            # small working set so an active session cannot grow without bound.
            if len(self._base_frames) > 8:
                oldest = next(iter(self._base_frames))
                if oldest != key:
                    del self._base_frames[oldest]
        image = base.copy()
        draw = ImageDraw.Draw(image, "RGBA")
        ink = (177, 196, 225, 228)
        accent = (101, 183, 206, 220)
        warn = (222, 176, 108, 235)
        bad = (224, 119, 126, 235)
        # One buddy, one bounded anchor.  It is a scene entity, never a pane.
        if visual.semantic_state != "hidden":
            start_fx, start_fy = scene.anchors.get(
                visual.previous_anchor, scene.anchors["center"]
            )
            end_fx, end_fy = scene.anchors.get(
                scene.buddy_anchor, scene.anchors["center"]
            )
            # Ease between semantic anchors.  The event chooses the target;
            # the clock only supplies presentation time.
            progress = min(max(visual.transition, 0.0), 1.0)
            eased = progress * progress * (3.0 - 2.0 * progress)
            fx = start_fx + (end_fx - start_fx) * eased
            fy = start_fy + (end_fy - start_fy) * eased
            bx, by = int(width * fx), int(height * fy)
            bob = 0 if progress >= 1 else int((1 - progress) * 10)
            by += bob
            self._draw_buddy(draw, bx, by, max(18, width // 28), scene.status, visual.phase, ink, accent, warn, bad)

        encoded = io.BytesIO()
        # Animation ticks reuse the cached scene layer and use a low-latency
        # PNG encode. Compression is still lossless; ``optimize=True`` is a
        # costly palette/scan optimisation that should happen only for a
        # deliberate asset export, not for a live frame transport.
        image.convert("RGB").save(encoded, format="PNG", optimize=False, compress_level=1)
        return FrameBuffer(encoded.getvalue(), width, height)

    def _draw_buddy(self, draw: Any, x: int, y: int, scale: int, status: str, phase: float, ink: Color, accent: Color, warn: Color, bad: Color) -> None:
        """Draw one small scene character with restrained state cues.

        Buddy is deliberately an entity in the OI scene, not a second pane or
        a text dashboard.  The pose stays stable while the semantic state
        chooses the accent and a few bounded presentation details.
        """
        status = str(status).upper()
        color = bad if status in {"FAILURE", "BLOCKED"} else warn if status == "APPROVAL" else accent
        stroke = max(1, scale // 6)
        body_w = scale * 4
        body_top = y - scale * 2
        body_bottom = y + scale * 2
        screen_top = y - scale
        screen_bottom = y + scale // 2
        draw.rounded_rectangle(
            (x - body_w, body_top, x + body_w, body_bottom),
            radius=max(2, scale // 2), outline=color,
            fill=(16, 28, 51, 238), width=stroke,
        )
        # Monitor face and phosphor eyes.  A phase offset gives the eyes a
        # tiny scan/breathing movement without changing scene semantics.
        draw.rounded_rectangle(
            (x - scale * 2, screen_top, x + scale * 2, screen_bottom),
            radius=max(1, scale // 4), outline=(100, 148, 191, 180),
            fill=(9, 20, 39, 245), width=stroke,
        )
        eye_y = y - scale // 3 + (1 if phase > 0.72 else 0)
        eye = bad if status in {"FAILURE", "BLOCKED"} else ink
        draw.ellipse((x - scale * 2 + stroke, eye_y, x - scale + stroke, eye_y + max(2, scale // 2)), fill=eye)
        draw.ellipse((x + scale - stroke, eye_y, x + scale * 2 - stroke, eye_y + max(2, scale // 2)), fill=eye)
        # Antenna and feet make the silhouette readable at small CRT sizes.
        antenna_x = x + (scale if phase > 0.5 else -scale)
        draw.line((x, body_top, antenna_x, y - scale * 4), fill=color, width=stroke)
        draw.ellipse((antenna_x - stroke, y - scale * 4 - stroke, antenna_x + stroke, y - scale * 4 + stroke), fill=color)
        draw.line((x - scale * 2, body_bottom, x - scale * 3, body_bottom + scale), fill=color, width=stroke)
        draw.line((x + scale * 2, body_bottom, x + scale * 3, body_bottom + scale), fill=color, width=stroke)

        # State-specific props stay inside the OI viewport: an execution
        # pulse, an approval card, or a failure marker.  These are visual
        # explanations of canonical state, never synthetic progress.
        if status in {"EXECUTING", "READING", "SEARCHING"}:
            pulse = int((phase % 1.0) * scale * 3)
            draw.arc((x - body_w - pulse, y - scale * 3 - pulse, x + body_w + pulse, y + scale * 3 + pulse), 200, 340, fill=color, width=stroke)
        elif status == "APPROVAL":
            card_x = x + body_w + scale
            draw.rounded_rectangle((card_x, y - scale, card_x + scale * 4, y + scale * 2), radius=2, outline=warn, fill=(30, 35, 54, 245), width=stroke)
            draw.line((card_x + scale, y, card_x + scale * 3, y), fill=warn, width=stroke)
        elif status in {"FAILURE", "BLOCKED"}:
            draw.line((x + body_w + scale, y - scale, x + body_w + scale * 2, y + scale), fill=bad, width=stroke)
            draw.line((x + body_w + scale * 2, y - scale, x + body_w + scale, y + scale), fill=bad, width=stroke)

        self._text(draw, (x - body_w, body_bottom + scale + 1), status.lower(), self._font(max(9, scale // 2)), color)


__all__ = ["FrameBuffer", "OIFrameBuffer", "pillow_available"]
