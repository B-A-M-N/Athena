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

from athena.cli.activity import VisualActionKind
from athena.cli.animation import OIVisualState
from athena.cli.scene import OIScene, tree_rows
from athena.cli.render.scene import _diagnostic_lines, format_progress

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

    _FONT_PATHS = (
        "/usr/share/fonts/opentype/fira/FiraMono-Regular.otf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
    )

    def __init__(self, *, font_path: str | None = None) -> None:
        self.font_path = font_path
        self._fonts: dict[int, Any] = {}
        self._max_font_entries = 32
        self._base_frames: dict[tuple[int, int, tuple[Any, ...]], Any] = {}
        self._base_frame_sizes: dict[tuple[int, int, tuple[Any, ...]], int] = {}
        self._base_frame_bytes = 0
        self._base_png: dict[tuple[int, int, tuple[Any, ...]], bytes] = {}
        self._base_png_bytes = 0
        self._max_cache_bytes = 16 * 1024 * 1024

    def _trim_base_caches(self, protected_key: tuple[Any, ...] | None = None) -> None:
        """Keep framebuffer caches bounded by entries and combined memory."""
        while (
            len(self._base_frames) > 8
            or len(self._base_png) > 8
            or self._base_frame_bytes + self._base_png_bytes > self._max_cache_bytes
        ):
            frame_candidate = next(
                (key for key in self._base_frames if key != protected_key), None
            )
            png_candidate = next((key for key in self._base_png if key != protected_key), None)
            if frame_candidate is None and png_candidate is None:
                break
            if len(self._base_frames) > 8 or (
                self._base_frame_bytes >= self._base_png_bytes and frame_candidate is not None
            ):
                assert frame_candidate is not None
                self._base_frames.pop(frame_candidate, None)
                self._base_frame_bytes -= self._base_frame_sizes.pop(frame_candidate, 0)
            else:
                assert png_candidate is not None
                png = self._base_png.pop(png_candidate, None)
                if png is not None:
                    self._base_png_bytes -= len(png)

    def _font(self, size: int):
        if ImageFont is None:
            return None
        size = max(int(size), 8)
        font = self._fonts.pop(size, None)
        if font is not None:
            self._fonts[size] = font
            return font
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
        if len(self._fonts) > self._max_font_entries:
            self._fonts.pop(next(iter(self._fonts)))
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
        base = self._base_frames.pop(key, None)
        if base is not None:
            self._base_frames[key] = base
        if base is None:
            base = self._render_base(scene, width, height)
            self._base_frames[key] = base
            size = width * height * 4
            self._base_frame_sizes[key] = size
            self._base_frame_bytes += size
            self._trim_base_caches(protected_key=key)
        return base, key

    @staticmethod
    def _buddy_position(
        scene: OIScene, visual: OIVisualState, width: int, height: int
    ) -> tuple[int, int, int]:
        start_fx, start_fy = scene.anchors.get(visual.previous_anchor, scene.anchors["center"])
        end_fx, end_fy = scene.anchors.get(scene.buddy_anchor, scene.anchors["center"])
        progress = min(max(visual.transition, 0.0), 1.0)
        eased = progress * progress * (3.0 - 2.0 * progress)
        fx = start_fx + (end_fx - start_fx) * eased
        fy = start_fy + (end_fy - start_fy) * eased
        scale = max(18, width // 28)
        return (
            int(width * fx),
            int(height * fy) + (0 if progress >= 1 else int((1 - progress) * 10)),
            scale,
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
    def _text(
        draw: Any, xy: tuple[int, int], text: object, font: Any, fill: Color, *, spacing: int = 2
    ) -> None:
        draw.text(xy, str(text), font=font, fill=fill, spacing=spacing)

    def _render_base(self, scene: OIScene, width: int, height: int) -> Any:
        """Render everything that does not change during an animation tick."""
        image = Image.new("RGBA", (width, height), (9, 15, 31, 255))
        draw = ImageDraw.Draw(image, "RGBA")

        # Glass depth: vignette-like bands, a faint perspective grid, and
        # scanlines. These are static atmosphere, not semantic state.
        for y in range(height):
            mix = y / max(height - 1, 1)
            draw.line(
                (0, y, width, y),
                fill=(10 + int(4 * mix), 17 + int(9 * mix), 35 + int(15 * mix), 255),
            )
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

        # Trace annotations: truthful, derived from canonical events.
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
                for line in _diagnostic_lines(diagnostic):
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
        draw = ImageDraw.Draw(image, "RGBA")
        ink = (177, 196, 225, 228)
        accent = (101, 183, 206, 220)
        warn = (222, 176, 108, 235)
        bad = (224, 119, 126, 235)
        # One buddy, one bounded anchor.  It is a scene entity, never a pane.
        if visual.semantic_state != "hidden":
            bx, by, scale = self._buddy_position(scene, visual, width, height)
            self._draw_buddy(
                draw,
                bx,
                by,
                scale,
                scene.status,
                visual.phase,
                ink,
                accent,
                warn,
                bad,
                character=scene.character,
                mode=scene.mode,
            )

        encoded = io.BytesIO()
        # Animation ticks reuse the cached scene layer and use a low-latency
        # PNG encode. Compression is still lossless; ``optimize=True`` is a
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
        png = self._base_png.pop(key, None)
        if png is not None:
            self._base_png[key] = png
        if png is None:
            encoded = io.BytesIO()
            base.convert("RGB").save(encoded, format="PNG", optimize=False, compress_level=1)
            png = encoded.getvalue()
            self._base_png[key] = png
            self._base_png_bytes += len(png)
            self._trim_base_caches(protected_key=key)
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
                include(margin, body_top + 45 + visible * 16, width - margin, body_top + 60 + visible * 16)

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
        bx, by, scale = self._buddy_position(scene, visual, width, height)
        margin = max(scale * 2, 8)
        left = max(0, bx - scale * 5 - margin)
        top = max(0, by - scale * 5 - margin)
        right = min(width, bx + scale * 7 + margin)
        bottom = min(height, by + scale * 5 + margin)
        # Align to the approximate terminal cell grid used by the caller so a
        # clipped image scales predictably in a cell placement.
        cell_width, cell_height = 10, 20
        left = (left // cell_width) * cell_width
        top = (top // cell_height) * cell_height
        right = min(width, max(right, left + cell_width))
        bottom = min(height, max(bottom, top + cell_height))
        image = Image.new("RGBA", (right - left, bottom - top), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image, "RGBA")
        ink = (177, 196, 225, 228)
        accent = (101, 183, 206, 220)
        warn = (222, 176, 108, 235)
        bad = (224, 119, 126, 235)
        self._draw_buddy(
            draw,
            bx - left,
            by - top,
            scale,
            scene.status,
            visual.phase,
            ink,
            accent,
            warn,
            bad,
            character=scene.character,
            mode=scene.mode,
        )
        encoded = io.BytesIO()
        image.save(encoded, format="PNG", optimize=False, compress_level=1)
        return FrameBuffer(
            encoded.getvalue(),
            width,
            height,
            dirty_region=(left, top, right - left, bottom - top),
            layer="overlay",
            base_key=(width, height, self._scene_key(scene)),
        )

    def _draw_buddy(
        self,
        draw: Any,
        x: int,
        y: int,
        scale: int,
        status: str,
        phase: float,
        ink: Color,
        accent: Color,
        warn: Color,
        bad: Color,
        *,
        character: str = "owl",
        mode: VisualActionKind = VisualActionKind.IDLE,
    ) -> None:
        """Draw one small scene character with restrained state cues.

        Buddy is deliberately an entity in the OI scene, not a second pane or
        a text dashboard.  The pose stays stable while the semantic state
        chooses the accent and a few bounded presentation details.

        Geometry is quantized to a fixed logical sprite grid before scaling
        so the silhouette reads as a discrete CRT/pixel-grid treatment rather
        than a smooth vector graphic.
        """
        status = str(status).upper()
        color = (
            bad if status in {"FAILURE", "BLOCKED"} else warn if status == "APPROVAL" else accent
        )
        # Sprite grid: 16 wide x 12 tall logical cells.
        grid_w, grid_h = 16, 12
        cell = max(scale // 2, 4)
        stroke = max(1, cell // 3)
        body_w = grid_w * cell
        body_h = grid_h * cell
        # Quantize anchor to grid center.
        gx, gy = x - body_w // 2, y - body_h // 2

        def px(grid_x: int, grid_y: int) -> tuple[int, int]:
            return (gx + grid_x * cell, gy + grid_y * cell)

        def pixel_rect(gx0: int, gy0: int, gx1: int, gy1: int, fill: Color) -> None:
            draw.rectangle(
                (px(gx0, gy0), px(gx1, gy1)),
                fill=fill,
                outline=None,
                width=0,
            )

        def pixel(gx0: int, gy0: int, gx1: int, gy1: int, fill: Color) -> None:
            draw.line((px(gx0, gy0), px(gx1, gy1)), fill=fill, width=stroke)

        character = str(character or "owl").casefold()
        body_color = (16, 28, 51, 238)

        # --- Character-specific body silhouette on the 16x12 grid ---
        if character == "owl":
            # Owl silhouette: rounded head with ear tufts, wide body,
            # two eyes, small beak, wing folds, and small feet.
            # No monitor face or antenna — a genuine owl shape.
            # Head top (rows 2-3): narrower, rounded shape.
            pixel_rect(2, 2, 6, 2, body_color)  # left head top
            pixel_rect(9, 2, 13, 2, body_color)  # right head top
            pixel_rect(1, 3, 7, 3, body_color)  # left head wide
            pixel_rect(8, 3, 14, 3, body_color)  # right head wide
            # Head-body (rows 4-10): wider body merging from head.
            pixel_rect(2, 4, 7, 10, body_color)  # left body+head
            pixel_rect(8, 4, 14, 10, body_color)  # right body+head
            # Outline: perimeter lines for the owl silhouette.
            pixel(2, 2, 1, 3, color)  # left head curve top
            pixel(1, 3, 1, 10, color)  # left body side
            pixel(1, 10, 2, 11, color)  # left foot up
            pixel(2, 11, 3, 11, color)  # left foot
            pixel(13, 2, 14, 3, color)  # right head curve top
            pixel(14, 3, 14, 10, color)  # right body side
            pixel(14, 10, 13, 11, color)  # right foot up
            pixel(12, 11, 11, 11, color)  # right foot
            # Ear tufts: upward-pointing triangles.
            pixel(4, 2, 3, 0, color)  # left ear slope
            pixel(5, 2, 3, 1, color)  # left ear inner
            pixel_rect(3, 0, 3, 1, body_color)  # left ear tip
            pixel(12, 2, 13, 0, color)  # right ear slope
            pixel(11, 2, 13, 1, color)  # right ear inner
            pixel_rect(12, 0, 12, 1, body_color)  # right ear tip
            # Eyes: quantized to grid, shift with phase.
            eye_y = 5
            eye_shift = 0
            if status in {"READING", "SEARCHING", "INSPECTING"}:
                eye_shift = 1 if phase > 0.5 else -1
            elif status in {"FAILURE", "BLOCKED"}:
                eye_shift = -1
            eye = bad if status in {"FAILURE", "BLOCKED"} else ink
            pixel_rect(5 + eye_shift, eye_y, 6 + eye_shift, eye_y + 1, eye)
            pixel_rect(9 - eye_shift, eye_y, 10 - eye_shift, eye_y + 1, eye)
            # Beak: small triangle between eyes below face.
            pixel_rect(7, 8, 8, 8, warn)
            # Wing cues: subtle diagonal folds at body sides.
            pixel(2, 4, 0, 6, color)  # left wing
            pixel(14, 4, 16, 6, color)  # right wing
            # Feet.
            pixel(3, 11, 2, 11, color)
            pixel(12, 11, 13, 11, color)
        elif character == "cat":
            # Original rectangular body with cat ears on top.
            body_color = (16, 28, 51, 238)
            pixel_rect(2, 2, 14, 10, body_color)
            pixel(2, 2, 14, 2, color)
            pixel(2, 10, 14, 10, color)
            pixel(2, 2, 2, 10, color)
            pixel(14, 2, 14, 10, color)
            # Monitor face.
            pixel_rect(4, 3, 12, 8, (9, 20, 39, 245))
            pixel(4, 3, 12, 3, (100, 148, 191, 180))
            pixel(4, 8, 12, 8, (100, 148, 191, 180))
            pixel(4, 3, 4, 8, (100, 148, 191, 180))
            pixel(12, 3, 12, 8, (100, 148, 191, 180))
            # Eyes: quantized to grid, shift with phase.
            eye_y = 5
            eye_shift = 0
            if status in {"READING", "SEARCHING", "INSPECTING"}:
                eye_shift = 1 if phase > 0.5 else -1
            elif status in {"FAILURE", "BLOCKED"}:
                eye_shift = -1
            eye = bad if status in {"FAILURE", "BLOCKED"} else ink
            pixel_rect(6 + eye_shift, eye_y, 7 + eye_shift, eye_y + 1, eye)
            pixel_rect(9 - eye_shift, eye_y, 10 - eye_shift, eye_y + 1, eye)
            # Antenna.
            antenna_x = 4 if phase > 0.5 else 12
            pixel(8, 2, antenna_x, 0, color)
            pixel_rect(antenna_x - 1, 0, antenna_x + 1, 1, color)
            # Feet.
            pixel(4, 10, 2, 11, color)
            pixel(12, 10, 14, 11, color)
            # Pointed cat ears.
            pixel(4, 2, 3, 0, color)
            pixel(12, 2, 13, 0, color)
        else:
            # Default rectangular body with monitor face for unknown/custom chars.
            pixel_rect(2, 2, 14, 10, body_color)
            pixel(2, 2, 14, 2, color)
            pixel(2, 10, 14, 10, color)
            pixel(2, 2, 2, 10, color)
            pixel(14, 2, 14, 10, color)
            pixel_rect(4, 3, 12, 8, (9, 20, 39, 245))
            pixel(4, 3, 12, 3, (100, 148, 191, 180))
            pixel(4, 8, 12, 8, (100, 148, 191, 180))
            pixel(4, 3, 4, 8, (100, 148, 191, 180))
            pixel(12, 3, 12, 8, (100, 148, 191, 180))
            eye_y = 5
            eye_shift = 0
            if status in {"READING", "SEARCHING", "INSPECTING"}:
                eye_shift = 1 if phase > 0.5 else -1
            elif status in {"FAILURE", "BLOCKED"}:
                eye_shift = -1
            eye = bad if status in {"FAILURE", "BLOCKED"} else ink
            pixel_rect(6 + eye_shift, eye_y, 7 + eye_shift, eye_y + 1, eye)
            pixel_rect(9 - eye_shift, eye_y, 10 - eye_shift, eye_y + 1, eye)
            antenna_x = 4 if phase > 0.5 else 12
            pixel(8, 2, antenna_x, 0, color)
            pixel_rect(antenna_x - 1, 0, antenna_x + 1, 1, color)
            pixel(4, 10, 2, 11, color)
            pixel(12, 10, 14, 11, color)
        # Mode-specific pose.
        if mode is VisualActionKind.CODE:
            pixel(2, 6, 0, 7, color)
            pixel(14, 6, 16, 7, color)
        elif mode in {VisualActionKind.TEST, VisualActionKind.VERIFY}:
            pixel(14, 4, 16, 3, color)
        # State-specific cues.
        if status == "THINKING":
            # Arc around body.
            pixel(1, 1, 2, 0, color)
            pixel(14, 1, 15, 0, color)
        elif status in {"READING", "SEARCHING", "INSPECTING"}:
            scan_y = 3 + int((phase % 1.0) * 5)
            pixel(4, scan_y, 12, scan_y, color)
        elif status in {"EXECUTING", "DELEGATED"}:
            pulse = int((phase % 1.0) * 3)
            pixel(2 - pulse, 2, 2 - pulse, 10, color)
            pixel(14 + pulse, 2, 14 + pulse, 10, color)
            if status == "DELEGATED":
                pixel(15, 6, 18, 3, color)
                pixel(15, 6, 18, 9, color)
        elif status == "APPROVAL":
            # The approval affordance shares the same logical sprite grid;
            # avoid a smooth vector card that would make this one state look
            # like a different visual system.
            pixel_rect(16, 4, 20, 8, (30, 35, 54, 245))
            pixel(16, 4, 20, 4, warn)
            pixel(20, 4, 20, 8, warn)
            pixel(20, 8, 16, 8, warn)
            pixel(16, 8, 16, 4, warn)
            pixel(17, 6, 19, 6, warn)
        elif status in {"FAILURE", "BLOCKED"}:
            pixel(16, 4, 19, 8, bad)
            pixel(19, 4, 16, 8, bad)
            pixel(-2, 8, 1, 10, bad)
        elif status == "RECOVERING":
            pixel(-2, 5, -1, 3, color)
            pixel(-1, 3, 2, 1, color)
            pixel(2, 1, 6, 1, color)
            pixel(6, 1, 8, 3, color)
            pixel(-2, 5, -4, 5, color)
        elif status in {"SUCCESS", "COMPLETE"}:
            pixel(-1, 3, 1, 1, color)
            pixel(1, 1, 5, 1, color)
            pixel(5, 1, 7, 3, color)
            pixel(16, 6, 18, 8, color)
            pixel(18, 8, 22, 3, color)
        elif status in {"GENERATED", "GENERATED_TOOL", "CONSTRUCTING"}:
            pixel(16, 6, 18, 4, color)
            pixel(18, 4, 20, 6, color)
            pixel(20, 6, 18, 8, color)
            pixel(18, 8, 16, 6, color)
            pixel(18, 4, 18, 8, color)

        self._text(
            draw,
            (gx + 2 * cell, gy + grid_h * cell + 1),
            status.lower(),
            self._font(max(9, cell)),
            color,
        )


__all__ = ["FrameBuffer", "OIFrameBuffer", "pillow_available"]
