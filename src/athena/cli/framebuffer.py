"""Pillow-backed OI framebuffer for the Glass CRT viewport.

The framebuffer is intentionally scene-only: it receives a fixed viewport and
cannot affect chassis geometry.  If Pillow is not installed, callers fall back
to the ANSI OI scene automatically.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any, TypeAlias

from athena.cli.animation import OIVisualState
from athena.cli.framebuffer_cache import FramebufferCache
from athena.cli.framebuffer_layout import BuddyPlacement
from athena.cli.framebuffer_world import BuddyWorld
from athena.presentation.ansi_scene import diagnostic_lines, format_progress
from athena.presentation.scene import OIScene, tree_rows
from athena.presentation.semantics import VisualActionKind

Image: Any = None
ImageDraw: Any = None
ImageFilter: Any = None
ImageFont: Any = None
try:  # Optional so plain/ANSI installs remain lightweight.
    from PIL import Image as _Image
    from PIL import ImageDraw as _ImageDraw
    from PIL import ImageFilter as _ImageFilter
    from PIL import ImageFont as _ImageFont

    Image = _Image
    ImageDraw = _ImageDraw
    ImageFilter = _ImageFilter
    ImageFont = _ImageFont
except ImportError:  # pragma: no cover - exercised in minimal installs
    pass


Color: TypeAlias = tuple[int, int, int] | tuple[int, int, int, int]


class _OffsetDraw:
    """Translate drawing coordinates into a cropped dirty-region image."""

    def __init__(self, draw: Any, left: int, top: int) -> None:
        self._draw = draw
        self._left = left
        self._top = top

    def _point(self, point: tuple[int, int]) -> tuple[int, int]:
        return point[0] - self._left, point[1] - self._top

    def _box(self, box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        return (
            box[0] - self._left,
            box[1] - self._top,
            box[2] - self._left,
            box[3] - self._top,
        )

    def line(self, xy, **kwargs) -> None:
        values = tuple(xy)
        if len(values) == 4 and all(isinstance(value, (int, float)) for value in values):
            points = ((values[0], values[1]), (values[2], values[3]))
        else:
            points = values
        self._draw.line([self._point(point) for point in points], **kwargs)

    def rectangle(self, xy, **kwargs) -> None:
        self._draw.rectangle(self._box(tuple(xy)), **kwargs)

    def arc(self, xy, *args, **kwargs) -> None:
        self._draw.arc(self._box(tuple(xy)), *args, **kwargs)

    def text(self, xy, text, **kwargs) -> None:
        self._draw.text(self._point(tuple(xy)), text, **kwargs)


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
    dirty_region: tuple[int, int, int, int] | None = None
    layer: str = "full"
    base_key: tuple[Any, ...] | None = None


class OIFrameBuffer:
    """Render a restrained blue-black computational world as PNG."""

    # Collision uses the complete bounded terrain and owl rectangle.
    BUDDY_WORLD_WIDTH = BuddyWorld.WIDTH
    BUDDY_WORLD_HEIGHT = BuddyWorld.HEIGHT
    OWL_RENDER_WIDTH = BuddyWorld.OWL_WIDTH
    OWL_RENDER_HEIGHT = BuddyWorld.OWL_HEIGHT

    def __init__(self, *, font_path: str | None = None) -> None:
        self.font_path = font_path
        self._cache = FramebufferCache(font_path=font_path, image_font=ImageFont)
        self._world = BuddyWorld(Image, ImageDraw, ImageFilter)

    def _trim_base_caches(self, protected_key: tuple[Any, ...] | None = None) -> None:
        """Keep framebuffer caches bounded by entries and combined memory."""
        self._cache.trim(protected_key)

    def _font(self, size: int):
        return self._cache.font(size)

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
            scene.character,
            scene.mode.value,
            repr(scene.code_view),
            tuple(repr(item) for item in scene.diagnostics),
            tuple(repr(item) for item in scene.verification_checks),
            tuple(sorted((str(key), repr(value)) for key, value in scene.progress.items())),
            entities,
            tuple(scene.alerts),
            repr(scene.workspace_tree),
            repr(scene.runtime_tree),
            tuple(scene.trace),
            scene.model_provider,
            scene.model,
            scene.model_role,
            scene.model_request_id,
            scene.model_request_status,
        )

    def _base_image(self, scene: OIScene, width: int, height: int) -> tuple[Any, tuple[Any, ...]]:
        """Return a cached opaque scene layer and its stable content key."""
        key = (width, height, self._scene_key(scene))
        base = self._cache.base_image(key, lambda: self._render_base(scene, width, height))
        return base, key

    @classmethod
    def _buddy_position(cls, scene, visual, width, height):
        return BuddyPlacement.position(scene, visual, width, height)

    @classmethod
    def _buddy_overlaps_content(cls, scene, left, top, width, height):
        return BuddyPlacement.overlaps_content(scene, left, top, width, height)

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
    def _text(
        draw: Any, xy: tuple[int, int], text: object, font: Any, fill: Color, *, spacing: int = 2
    ) -> None:
        draw.text(xy, str(text), font=font, fill=fill, spacing=spacing)

    def _render_base(self, scene: OIScene, width: int, height: int) -> Any:
        """Render everything that does not change during an animation tick."""
        image = Image.new("RGBA", (width, height), (9, 15, 31, 255))
        draw = ImageDraw.Draw(image, "RGBA")

        # Glass depth: a blue-black gradient plus restrained scanlines. The
        # animated perspective terrain belongs to the bounded mascot world.
        for y in range(height):
            mix = y / max(height - 1, 1)
            draw.line(
                (0, y, width, y),
                fill=(10 + int(4 * mix), 17 + int(9 * mix), 35 + int(15 * mix), 255),
            )
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
        self._text(
            draw,
            (margin, top + 22),
            f"> MODEL REQUEST · {scene.model_request_label}",
            font_small,
            dim,
        )
        self._text(
            draw,
            (margin, top + 40),
            "> MODEL REQUEST ACTIVE" if scene.model_request_status == "active" else "> IDLE",
            font_small,
            accent,
        )
        draw.line((margin, top + 60, width - margin, top + 60), fill=(115, 154, 196, 95), width=1)

        if scene.mode in {
            VisualActionKind.CODE,
            VisualActionKind.TEST,
            VisualActionKind.VERIFY,
            VisualActionKind.FAILURE,
            VisualActionKind.SEARCH,
            VisualActionKind.APPROVAL,
            VisualActionKind.RECOVER,
            VisualActionKind.GENERATE,
        }:
            self._render_action_content(
                draw,
                scene,
                width,
                height,
                margin,
                top + 78,
                font_small,
                font,
                ink,
                dim,
                bright,
                accent,
                warn,
                bad,
            )
            draw.rectangle((2, 2, width - 3, height - 3), outline=(125, 157, 198, 68), width=1)
            return image

        left = margin
        right = width // 2 + 4
        body_top = top + 78
        self._text(draw, (left, body_top), "WORKSPACE MAP", font_small, dim)
        self._text(draw, (right, body_top), "RUNTIME TREE", font_small, dim)
        draw.line(
            (width // 2, body_top - 4, width // 2, height - 52), fill=(92, 125, 165, 50), width=1
        )

        workspace_rows = tree_rows(scene.workspace_tree)
        if workspace_rows:
            for idx, (prefix, node) in enumerate(workspace_rows[:8]):
                label = f"{prefix}{_state_marker(node.status)} {node.label}"
                self._text(
                    draw,
                    (left, body_top + 24 + idx * 20),
                    label[: max(20, width // 18)],
                    font,
                    self._entity_color(node, ink, accent, warn, bad),
                )
        else:
            self._text(
                draw, (left, body_top + 42), "· no workspace resources observed", font_small, dim
            )

        runtime_rows = tree_rows(scene.runtime_tree)
        if runtime_rows:
            for idx, (prefix, node) in enumerate(runtime_rows[:8]):
                label = f"{prefix}{_state_marker(node.status)} {node.label}"
                self._text(
                    draw,
                    (right, body_top + 24 + idx * 20),
                    label[: max(20, width // 18)],
                    font,
                    self._entity_color(node, ink, accent, warn, bad),
                )
        else:
            self._text(
                draw, (right, body_top + 42), "· no runtime operations observed", font_small, dim
            )

        # Stream/alert band stays subordinate to the scene. It still exposes
        # live data, but the graphical OI is not just a firehose.
        band_y = height - 102
        draw.line((margin, band_y, width - margin, band_y), fill=(115, 154, 196, 75), width=1)
        self._text(draw, (margin, band_y + 10), "LIVE TRACE", font_small, dim)
        # Live stream text belongs to the dynamic motion/content layer.  It
        # must not invalidate the retained CRT/background scene PNG.
        entries = scene.alerts[-2:] or ["awaiting canonical events"]
        for idx, entry in enumerate(entries):
            color = (
                bad
                if "fail" in entry.lower() or "error" in entry.lower()
                else warn
                if "approval" in entry.lower()
                else ink
            )
            self._text(
                draw,
                (margin, band_y + 30 + idx * 18),
                entry[: max(20, width // 12)],
                font_small,
                color,
            )

        # Very faint corner glass highlights make the CRT read as glass without
        # obscuring text or pretending to be a full-screen screenshot.
        draw.arc((width - 110, -45, width + 48, 76), 168, 286, fill=(200, 224, 255, 20), width=2)
        draw.rectangle((2, 2, width - 3, height - 3), outline=(125, 157, 198, 68), width=1)
        return image

    def _render_action_content(
        self,
        draw: Any,
        scene: OIScene,
        width: int,
        height: int,
        margin: int,
        body_top: int,
        font_small: Any,
        font: Any,
        ink: Color,
        dim: Color,
        bright: Color,
        accent: Color,
        warn: Color,
        bad: Color,
    ) -> None:
        """Render the same action-specific material exposed by the ANSI bridge."""
        code = scene.code_view
        target = code.path if code else "workspace"
        title = {
            VisualActionKind.CODE: f"CODE // {target}",
            VisualActionKind.TEST: f"TESTING // {target}",
            VisualActionKind.VERIFY: f"VERIFYING // {target}",
            VisualActionKind.FAILURE: "> RESULT: MISMATCH DETECTED",
            VisualActionKind.SEARCH: "SEARCHING // SYMBOL GRAPH",
            VisualActionKind.APPROVAL: "APPROVAL // OPERATION SCOPE",
            VisualActionKind.RECOVER: "RECOVERING // RETAINED EVIDENCE",
            VisualActionKind.GENERATE: "GENERATING // CAPABILITY",
        }[scene.mode]
        self._text(draw, (margin, body_top), title, font_small, bright)
        draw.line(
            (margin, body_top + 24, width - margin, body_top + 24),
            fill=(115, 154, 196, 75),
            width=1,
        )
        available_width = max(width - margin * 2, 1)
        row = body_top + 38
        row_height = max(16, int(getattr(font, "size", 12)) + 4)

        if scene.trace:
            for line in scene.trace[:3]:
                self._text(
                    draw, (margin, row), line[: max(1, available_width // 9)], font_small, dim
                )
                row += row_height
            row += row_height // 2

        if scene.mode is VisualActionKind.CODE and code is not None:
            state = code.mutation_state.upper() or "PROPOSED"
            self._text(draw, (margin, row), f"{code.language.upper()}  {state}", font_small, accent)
            row += row_height + 4
            lines = code.diff_hunks or code.lines
            for line in lines[: max((height - row - 30) // row_height, 1)]:
                prefix = line[:1]
                color = (
                    accent
                    if prefix == "+"
                    else bad
                    if prefix == "-"
                    else dim
                    if prefix == "@"
                    else ink
                )
                self._text(
                    draw, (margin, row), line[: max(1, available_width // 9)], font_small, color
                )
                row += row_height
            if code.preview_truncated:
                self._text(
                    draw, (margin, height - 42), "… preview bounded for display", font_small, warn
                )
        elif scene.mode is VisualActionKind.FAILURE:
            for diagnostic in scene.diagnostics[: max((height - row - 20) // row_height, 1)]:
                for line in diagnostic_lines(diagnostic):
                    self._text(
                        draw,
                        (margin, row),
                        line[: max(1, available_width // 9)],
                        font,
                        bad,
                    )
                    row += row_height
            if not scene.diagnostics:
                self._text(draw, (margin, row), "! no matching verification evidence", font, bad)
        elif scene.mode is VisualActionKind.VERIFY:
            checks = scene.verification_checks
            for check in checks[: max((height - row - 20) // row_height, 1)]:
                status = str(check.get("status") or "running").casefold()
                color = (
                    accent
                    if status in {"passed", "complete", "completed"}
                    else bad
                    if status in {"failed", "error"}
                    else warn
                )
                glyph = "✓" if color is accent else "!" if color is bad else "●"
                label = check.get("criterion") or check.get("check_id") or "check"
                self._text(draw, (margin, row), f"{glyph} {label}  {status}", font, color)
                row += row_height
            if not checks:
                self._text(draw, (margin, row), "● waiting for verification checks", font, warn)
        elif scene.mode is VisualActionKind.TEST:
            self._text(draw, (margin, row), "· impacted tests", font, dim)
            progress = scene.progress
            if progress.get("determinate") and progress.get("value") is not None:
                bar = format_progress(progress["value"], max(12, available_width // 9))
                self._text(
                    draw,
                    (margin, row + row_height),
                    bar,
                    font,
                    accent,
                )
                self._text(
                    draw,
                    (margin, row + row_height * 2),
                    progress.get("label") or "",
                    font_small,
                    dim,
                )
            else:
                self._text(
                    draw,
                    (margin, row + row_height),
                    progress.get("label") or "● running tests",
                    font,
                    accent,
                )
        elif scene.mode is VisualActionKind.SEARCH:
            for entity in scene.entities[: max((height - row - 20) // row_height, 1)]:
                self._text(
                    draw,
                    (margin, row),
                    f"· {entity.label}"[: max(1, available_width // 9)],
                    font,
                    ink,
                )
                row += row_height
        elif scene.mode is VisualActionKind.APPROVAL:
            approval = (
                scene.progress.get("approval")
                if isinstance(scene.progress.get("approval"), dict)
                else {}
            )
            self._text(draw, (margin, row), "APPROVAL REQUIRED", font, warn)
            row += row_height
            self._text(draw, (margin, row), f"? {target}  PAUSED", font, warn)
            if approval:
                self._text(
                    draw,
                    (margin, row + row_height),
                    str(approval.get("reason") or "choose a permitted scope"),
                    font_small,
                    ink,
                )
        elif scene.mode is VisualActionKind.RECOVER:
            self._text(draw, (margin, row), scene.status, font, warn)
            self._text(
                draw,
                (margin, row + row_height),
                "· retained evidence is being restored",
                font_small,
                ink,
            )
        elif scene.mode is VisualActionKind.GENERATE:
            self._text(draw, (margin, row), "· bounded generated capability", font, accent)
            if scene.stream:
                self._text(draw, (margin, row + row_height), scene.stream[-1], font_small, ink)

        band_y = height - 82
        draw.line((margin, band_y, width - margin, band_y), fill=(115, 154, 196, 75), width=1)
        self._text(draw, (margin, band_y + 9), "LIVE TRACE", font_small, dim)
        entries = scene.alerts[-2:] or scene.stream[-2:] or ["awaiting canonical events"]
        for index, entry in enumerate(entries):
            color = (
                bad
                if "fail" in entry.lower() or "error" in entry.lower()
                else warn
                if "approval" in entry.lower()
                else ink
            )
            self._text(
                draw,
                (margin, band_y + 27 + index * 17),
                entry[: max(20, width // 12)],
                font_small,
                color,
            )

    def render(
        self, scene: OIScene, visual: OIVisualState, width: int, height: int
    ) -> FrameBuffer | None:
        if Image is None:
            return None
        width, height = max(int(width), 80), max(int(height), 60)
        base, key = self._base_image(scene, width, height)
        image = base.copy()
        # One buddy, one bounded anchor.  It is a scene entity, never a pane.
        if visual.semantic_state != "hidden":
            position = self._buddy_position(scene, visual, width, height)
            if position is not None:
                left, top = position
                image.alpha_composite(
                    self._world.render(visual, status=scene.status),
                    dest=(left, top),
                )

        encoded = io.BytesIO()
        # Animation ticks reuse the cached scene layer and use a low-latency
        # PNG encode. Compression is lossless; ``optimize=True`` is a
        # costly palette/scan optimisation that should happen only for a
        # deliberate asset export, not for a live frame transport.
        image.convert("RGB").save(encoded, format="PNG", optimize=False, compress_level=1)
        return FrameBuffer(encoded.getvalue(), width, height, base_key=key)

    def render_base(self, scene: OIScene, width: int, height: int) -> FrameBuffer | None:
        """Encode only the opaque CRT layer for a stable Kitty placement."""
        if Image is None:
            return None
        width, height = max(int(width), 80), max(int(height), 60)
        base, key = self._base_image(scene, width, height)
        png = self._cache.cached_png(key)
        if png is None:
            encoded = io.BytesIO()
            base.convert("RGB").save(encoded, format="PNG", optimize=False, compress_level=1)
            png = encoded.getvalue()
            self._cache.save_png(key, png)
        return FrameBuffer(png, width, height, layer="base", base_key=key)

    def render_motion_overlay(
        self,
        scene: OIScene,
        visual: OIVisualState,
        width: int,
        height: int,
    ) -> FrameBuffer | None:
        """Encode the animated action layer independently from the scene base."""
        if Image is None:
            return None
        width, height = max(int(width), 80), max(int(height), 60)
        active = scene.mode is not VisualActionKind.IDLE or bool(scene.stream)
        if not active:
            return FrameBuffer(
                b"",
                width,
                height,
                layer="motion",
                base_key=(width, height, self._scene_key(scene)),
            )
        margin = max(18, width // 24)
        top = max(14, height // 22)
        body_top = top + 62
        ink = (177, 196, 225, 215)
        accent = (101, 183, 206, 220)
        warn = (222, 176, 108, 220)
        bad = (224, 119, 126, 230)
        mode = scene.mode

        # Compute the smallest rectangle that can contain this animation
        # layer.  A scanline-only frame is intentionally tiny; broad warning
        # and failure frames still occupy the full semantic region.
        left, top_bound, right, bottom = width, height, 0, 0

        def include(x1: int, y1: int, x2: int, y2: int) -> None:
            nonlocal left, top_bound, right, bottom
            left = min(left, max(0, x1))
            top_bound = min(top_bound, max(0, y1))
            right = max(right, min(width, x2))
            bottom = max(bottom, min(height, y2))

        # Moving scanner/data traces are presentation-only; their existence
        # follows the canonical action mode and never invents task progress.
        if mode in {
            VisualActionKind.SEARCH,
            VisualActionKind.READ,
            VisualActionKind.INSPECT,
            VisualActionKind.TEST,
            VisualActionKind.VERIFY,
            VisualActionKind.APPROVAL,
            VisualActionKind.RECOVER,
            VisualActionKind.GENERATE,
        }:
            scan_y = body_top + 28 + int(visual.scan_phase * max(height - body_top - 110, 1))
            include(margin, scan_y - 4, width - margin, scan_y + 5)
            for index in range(4):
                x = margin + int(
                    ((visual.activity_phase + index * 0.23) % 1.0) * max(width - margin * 2, 1)
                )

        if mode is VisualActionKind.CODE and scene.code_view is not None:
            view = scene.code_view
            source = view.diff_hunks or view.lines
            if source:
                visible = min(len(source), max(1, int(visual.code_reveal * len(source))))
                include(
                    margin,
                    body_top + 45 + visible * 16,
                    width - margin,
                    body_top + 60 + visible * 16,
                )

        if mode is VisualActionKind.FAILURE:
            include(margin, body_top + 28, width - margin, height - 92)
        elif mode is VisualActionKind.APPROVAL:
            include(margin, body_top + 28, width - margin, body_top + 92)
        elif mode is VisualActionKind.RECOVER:
            include(margin, body_top + 25, width - margin, height - 100)

        entries = scene.stream[-2:]
        if entries:
            include(margin, height - 74, width - margin, height - 35)

        if right <= left or bottom <= top_bound:
            return FrameBuffer(
                b"",
                width,
                height,
                layer="motion",
                base_key=(width, height, self._scene_key(scene)),
            )
        image = Image.new("RGBA", (right - left, bottom - top_bound), (0, 0, 0, 0))
        draw = _OffsetDraw(ImageDraw.Draw(image, "RGBA"), left, top_bound)

        if mode in {
            VisualActionKind.SEARCH,
            VisualActionKind.READ,
            VisualActionKind.INSPECT,
            VisualActionKind.TEST,
            VisualActionKind.VERIFY,
            VisualActionKind.APPROVAL,
            VisualActionKind.RECOVER,
            VisualActionKind.GENERATE,
        }:
            scan_y = body_top + 28 + int(visual.scan_phase * max(height - body_top - 110, 1))
            draw.line((margin, scan_y, width - margin, scan_y), fill=accent, width=2)
            for index in range(4):
                x = margin + int(
                    ((visual.activity_phase + index * 0.23) % 1.0) * max(width - margin * 2, 1)
                )
                draw.rectangle((x, scan_y - 3, x + 7, scan_y + 3), fill=(101, 183, 206, 150))

        if mode is VisualActionKind.CODE and scene.code_view is not None:
            view = scene.code_view
            source = view.diff_hunks or view.lines
            if source:
                visible = min(len(source), max(1, int(visual.code_reveal * len(source))))
                cursor_row = body_top + 58 + visible * 16
                cursor_x = margin + 2
                draw.line((cursor_x, cursor_row, width - margin, cursor_row), fill=accent, width=1)
                if visual.cursor_phase < 0.5:
                    draw.rectangle(
                        (cursor_x, cursor_row - 13, cursor_x + 8, cursor_row), fill=accent
                    )

        if mode is VisualActionKind.FAILURE:
            pulse = 0.5 + 0.5 * abs(visual.pulse_phase * 2.0 - 1.0)
            alpha = int(90 + 100 * pulse)
            draw.rectangle(
                (margin, body_top + 28, width - margin, height - 92),
                outline=(bad[0], bad[1], bad[2], alpha),
                width=2,
            )
        elif mode is VisualActionKind.APPROVAL:
            pulse = 0.5 + 0.5 * abs(visual.pulse_phase * 2.0 - 1.0)
            draw.rectangle(
                (margin, body_top + 28, width - margin, body_top + 92),
                outline=(warn[0], warn[1], warn[2], int(100 + 100 * pulse)),
                width=2,
            )
        elif mode is VisualActionKind.RECOVER:
            draw.arc(
                (margin, body_top + 25, width - margin, height - 100),
                int(visual.activity_phase * 360),
                int(visual.activity_phase * 360) + 210,
                fill=warn,
                width=2,
            )

        # Keep streamed evidence live without making it part of the retained
        # base. This is intentionally a bounded tail from ProjectionState.
        if entries:
            font = self._font(max(10, width // 64))
            band_y = height - 74
            draw.line((margin, band_y, width - margin, band_y), fill=(115, 154, 196, 95), width=1)
            self._text(draw, (margin, band_y + 8), "LIVE TRACE", font, ink)
            for index, entry in enumerate(entries):
                self._text(
                    draw,
                    (margin, band_y + 25 + index * 16),
                    entry[: max(20, width // 12)],
                    font,
                    ink,
                )

        encoded = io.BytesIO()
        image.save(encoded, format="PNG", optimize=False, compress_level=1)
        return FrameBuffer(
            encoded.getvalue(),
            right - left,
            bottom - top_bound,
            dirty_region=(left, top_bound, right - left, bottom - top_bound),
            layer="motion",
            base_key=(width, height, self._scene_key(scene)),
        )

    def render_overlay(
        self, scene: OIScene, visual: OIVisualState, width: int, height: int
    ) -> FrameBuffer | None:
        """Encode a clipped transparent Buddy layer for partial presentation.

        The static CRT remains resident in the host terminal. A stable overlay
        image id lets Kitty discard the previous Buddy placement before the new
        clipped rectangle is placed, so movement cannot leave stale pixels.
        """
        if Image is None:
            return None
        width, height = max(int(width), 80), max(int(height), 60)
        if visual.semantic_state == "hidden":
            return FrameBuffer(
                b"",
                width,
                height,
                layer="overlay",
                base_key=(width, height, self._scene_key(scene)),
            )
        position = self._buddy_position(scene, visual, width, height)
        if position is None:
            return FrameBuffer(
                b"",
                width,
                height,
                layer="overlay",
                base_key=(width, height, self._scene_key(scene)),
            )
        left, top = position
        world = self._world.render(visual, status=scene.status)
        encoded = io.BytesIO()
        world.save(encoded, format="PNG", optimize=False, compress_level=1)
        return FrameBuffer(
            encoded.getvalue(),
            width,
            height,
            dirty_region=(
                left,
                top,
                self.BUDDY_WORLD_WIDTH,
                self.BUDDY_WORLD_HEIGHT,
            ),
            layer="overlay",
            base_key=(width, height, self._scene_key(scene)),
        )


__all__ = ["FrameBuffer", "OIFrameBuffer", "pillow_available"]
