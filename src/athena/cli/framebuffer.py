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
from athena.cli.scene import OIScene

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
        self._base_frames: dict[tuple[int, int, tuple[Any, ...]], Any] = {}
        self._base_png: dict[tuple[int, int, tuple[Any, ...]], bytes] = {}

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
            scene.character,
            scene.mode.value,
            repr(scene.code_view),
            tuple(repr(item) for item in scene.diagnostics),
            tuple(repr(item) for item in scene.verification_checks),
            tuple(sorted((str(key), repr(value)) for key, value in scene.progress.items())),
            entities,
            tuple(scene.alerts),
        )

    def _base_image(self, scene: OIScene, width: int, height: int) -> tuple[Any, tuple[Any, ...]]:
        """Return a cached opaque scene layer and its stable content key."""
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
            f"MODE  {scene.status:<14}  VIEWPORT  {width}×{height}",
            font_small,
            dim,
        )
        draw.line((margin, top + 43, width - margin, top + 43), fill=(115, 154, 196, 95), width=1)

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
                top + 62,
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
        body_top = top + 62
        self._text(draw, (left, body_top), "WORKSPACE MAP", font_small, dim)
        self._text(draw, (right, body_top), "RUNTIME GRAPH", font_small, dim)
        draw.line(
            (width // 2, body_top - 4, width // 2, height - 52), fill=(92, 125, 165, 50), width=1
        )

        resources = [
            entity
            for entity in scene.entities
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
            entity
            for entity in scene.entities
            if entity.kind
            in {"operation", "child_task", "workflow", "verification", "generated_tool"}
        ]
        gx = right + (width - right) // 2
        if not runtime_entities:
            self._text(
                draw, (right, body_top + 42), "· no runtime operations observed", font_small, dim
            )
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
            draw.rounded_rectangle(
                (x - 54, y - 12, x + 54, y + 12),
                radius=4,
                outline=color,
                fill=(20, 35, 61, 175),
                width=1,
            )
            self._text(draw, (x - 45, y - 7), label[:18], font_small, color)

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
            VisualActionKind.FAILURE: "RESULT // MISMATCH DETECTED",
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
                message = (
                    diagnostic.get("message")
                    or diagnostic.get("detail")
                    or diagnostic.get("error")
                    or str(diagnostic)
                )
                location = (
                    diagnostic.get("path")
                    or diagnostic.get("file")
                    or diagnostic.get("location")
                    or ""
                )
                self._text(
                    draw,
                    (margin, row),
                    f"! {location} {message}".strip()[: max(1, available_width // 9)],
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
                value = min(max(float(progress["value"]), 0.0), 1.0)
                bar_width = max(12, available_width // 9)
                filled = int(bar_width * value)
                self._text(
                    draw,
                    (margin, row + row_height),
                    "[" + "█" * filled + "░" * (bar_width - filled) + "]",
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
        png = self._base_png.get(key)
        if png is None:
            encoded = io.BytesIO()
            base.convert("RGB").save(encoded, format="PNG", optimize=False, compress_level=1)
            png = encoded.getvalue()
            self._base_png[key] = png
            if len(self._base_png) > 8:
                oldest = next(iter(self._base_png))
                if oldest != key:
                    del self._base_png[oldest]
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
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image, "RGBA")
        margin = max(18, width // 24)
        top = max(14, height // 22)
        body_top = top + 62
        ink = (177, 196, 225, 215)
        accent = (101, 183, 206, 220)
        warn = (222, 176, 108, 220)
        bad = (224, 119, 126, 230)
        mode = scene.mode

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
                if visual.cursor_phase < 0.55:
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
        entries = scene.stream[-2:]
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
            width,
            height,
            dirty_region=(0, 0, width, height),
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
        """
        status = str(status).upper()
        color = (
            bad if status in {"FAILURE", "BLOCKED"} else warn if status == "APPROVAL" else accent
        )
        stroke = max(1, scale // 6)
        body_w = scale * 4
        body_top = y - scale * 2
        body_bottom = y + scale * 2
        screen_top = y - scale
        screen_bottom = y + scale // 2
        draw.rounded_rectangle(
            (x - body_w, body_top, x + body_w, body_bottom),
            radius=max(2, scale // 2),
            outline=color,
            fill=(16, 28, 51, 238),
            width=stroke,
        )
        # Monitor face and phosphor eyes.  A phase offset gives the eyes a
        # tiny scan/breathing movement without changing scene semantics.
        draw.rounded_rectangle(
            (x - scale * 2, screen_top, x + scale * 2, screen_bottom),
            radius=max(1, scale // 4),
            outline=(100, 148, 191, 180),
            fill=(9, 20, 39, 245),
            width=stroke,
        )
        eye_y = y - scale // 3 + (1 if phase > 0.72 else 0)
        eye_shift = 0
        if status in {"READING", "SEARCHING", "INSPECTING"}:
            eye_shift = int((phase - 0.5) * max(scale // 2, 1))
        elif status in {"FAILURE", "BLOCKED"}:
            eye_shift = -max(scale // 4, 1)
        eye = bad if status in {"FAILURE", "BLOCKED"} else ink
        eye_height = max(2, scale // 2)
        draw.ellipse(
            (
                x - scale * 2 + stroke + eye_shift,
                eye_y,
                x - scale + stroke + eye_shift,
                eye_y + eye_height,
            ),
            fill=eye,
        )
        draw.ellipse(
            (
                x + scale - stroke + eye_shift,
                eye_y,
                x + scale * 2 - stroke + eye_shift,
                eye_y + eye_height,
            ),
            fill=eye,
        )
        # Antenna and feet make the silhouette readable at small CRT sizes.
        antenna_x = x + (scale if phase > 0.5 else -scale)
        draw.line((x, body_top, antenna_x, y - scale * 4), fill=color, width=stroke)
        draw.ellipse(
            (
                antenna_x - stroke,
                y - scale * 4 - stroke,
                antenna_x + stroke,
                y - scale * 4 + stroke,
            ),
            fill=color,
        )
        draw.line(
            (x - scale * 2, body_bottom, x - scale * 3, body_bottom + scale),
            fill=color,
            width=stroke,
        )
        draw.line(
            (x + scale * 2, body_bottom, x + scale * 3, body_bottom + scale),
            fill=color,
            width=stroke,
        )

        # Character identity is shared with the ANSI mascot selection. Keep
        # the silhouette procedural and tiny, but make Glass honor the chosen
        # owl/cat/bot instead of rendering one robot for every choice.
        character = str(character or "owl").casefold()
        if character == "cat":
            draw.polygon(
                [
                    (x - scale * 2, screen_top),
                    (x - scale, screen_top - scale),
                    (x - scale // 2, screen_top),
                ],
                outline=color,
            )
            draw.polygon(
                [
                    (x + scale * 2, screen_top),
                    (x + scale, screen_top - scale),
                    (x + scale // 2, screen_top),
                ],
                outline=color,
            )
            draw.arc(
                (x + body_w, y, x + body_w + scale * 3, y + scale * 3),
                250,
                80,
                fill=color,
                width=stroke,
            )
        elif character == "owl":
            draw.line(
                (x - scale * 2, body_top, x - scale, body_top - scale), fill=color, width=stroke
            )
            draw.line(
                (x + scale * 2, body_top, x + scale, body_top - scale), fill=color, width=stroke
            )
            draw.polygon(
                [
                    (x, screen_bottom - stroke),
                    (x - stroke, screen_bottom + scale // 2),
                    (x + stroke, screen_bottom + scale // 2),
                ],
                fill=warn,
            )

        if mode is VisualActionKind.CODE:
            # Typing pose: bounded arms/data blocks, not a fake write timer.
            draw.line((x - body_w, y, x - body_w - scale * 2, y + scale), fill=color, width=stroke)
            draw.line((x + body_w, y, x + body_w + scale * 2, y + scale), fill=color, width=stroke)
            if phase < 0.65:
                draw.rectangle(
                    (x + body_w + scale * 2, y + scale, x + body_w + scale * 3, y + scale * 2),
                    fill=accent,
                )
        elif mode in {VisualActionKind.TEST, VisualActionKind.VERIFY}:
            draw.line(
                (x + body_w, y - scale, x + body_w + scale * 3, y - scale * 2),
                fill=color,
                width=stroke,
            )

        # State-specific clips stay inside the OI viewport. They are visual
        # explanations of canonical state, never synthetic progress.
        if status == "THINKING":
            antenna_x = x + (scale * 2 if phase > 0.5 else -scale * 2)
            draw.arc(
                (x - scale * 5, y - scale * 5, x + scale * 5, y + scale * 5),
                205,
                335,
                fill=color,
                width=stroke,
            )
        elif status in {"READING", "SEARCHING", "INSPECTING"}:
            scan_y = screen_top + int((phase % 1.0) * max(screen_bottom - screen_top, 1))
            draw.line((x - scale * 2, scan_y, x + scale * 2, scan_y), fill=color, width=stroke)
            if status == "SEARCHING":
                draw.arc(
                    (x + body_w, y - scale, x + body_w + scale * 3, y + scale * 2),
                    250,
                    80,
                    fill=color,
                    width=stroke,
                )
            elif status == "INSPECTING":
                draw.ellipse(
                    (x + body_w, y - scale * 2, x + body_w + scale * 2, y),
                    outline=color,
                    width=stroke,
                )
                draw.line(
                    (x + body_w + scale, y, x + body_w + scale * 2, y + scale),
                    fill=color,
                    width=stroke,
                )
        elif status in {"EXECUTING", "DELEGATED"}:
            pulse = int((phase % 1.0) * scale * 3)
            draw.arc(
                (
                    x - body_w - pulse,
                    y - scale * 3 - pulse,
                    x + body_w + pulse,
                    y + scale * 3 + pulse,
                ),
                200,
                340,
                fill=color,
                width=stroke,
            )
            if status == "DELEGATED":
                draw.line(
                    (x + body_w, y, x + body_w + scale * 2, y - scale * 2), fill=color, width=stroke
                )
                draw.line(
                    (x + body_w, y, x + body_w + scale * 2, y + scale * 2), fill=color, width=stroke
                )
        elif status == "APPROVAL":
            card_x = x + body_w + scale
            draw.rounded_rectangle(
                (card_x, y - scale, card_x + scale * 4, y + scale * 2),
                radius=2,
                outline=warn,
                fill=(30, 35, 54, 245),
                width=stroke,
            )
            draw.line((card_x + scale, y, card_x + scale * 3, y), fill=warn, width=stroke)
        elif status in {"FAILURE", "BLOCKED"}:
            draw.line(
                (x + body_w + scale, y - scale, x + body_w + scale * 2, y + scale),
                fill=bad,
                width=stroke,
            )
            draw.line(
                (x + body_w + scale * 2, y - scale, x + body_w + scale, y + scale),
                fill=bad,
                width=stroke,
            )
            draw.line(
                (x - body_w - scale, y + scale, x - body_w, y + scale * 2), fill=bad, width=stroke
            )
        elif status == "RECOVERING":
            draw.arc(
                (x - body_w - scale * 2, y - scale * 2, x + body_w + scale * 2, y + scale * 2),
                190,
                350,
                fill=color,
                width=stroke,
            )
            draw.line(
                (x - body_w - scale, y - scale, x - body_w - scale * 2, y), fill=color, width=stroke
            )
        elif status in {"SUCCESS", "COMPLETE"}:
            draw.arc(
                (x - body_w - scale, y - scale * 4, x + body_w + scale, y + scale * 4),
                190,
                350,
                fill=color,
                width=stroke,
            )
            draw.line(
                (x + body_w + scale, y, x + body_w + scale * 2, y + scale), fill=color, width=stroke
            )
            draw.line(
                (x + body_w + scale * 2, y + scale, x + body_w + scale * 4, y - scale),
                fill=color,
                width=stroke,
            )
        elif status in {"GENERATED", "GENERATED_TOOL", "CONSTRUCTING"}:
            cube_x = x + body_w + scale
            draw.polygon(
                [
                    (cube_x, y),
                    (cube_x + scale, y - scale // 2),
                    (cube_x + scale * 2, y),
                    (cube_x + scale, y + scale // 2),
                ],
                outline=color,
            )

        self._text(
            draw,
            (x - body_w, body_bottom + scale + 1),
            status.lower(),
            self._font(max(9, scale // 2)),
            color,
        )


__all__ = ["FrameBuffer", "OIFrameBuffer", "pillow_available"]
