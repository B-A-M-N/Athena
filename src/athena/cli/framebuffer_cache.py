"""Bounded font and retained-layer cache for the Pillow OI framebuffer."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


class FramebufferCache:
    """Own cache lifetime; drawing remains owned by ``OIFrameBuffer``."""

    _FONT_PATHS = (
        "/usr/share/fonts/opentype/fira/FiraMono-Regular.otf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
    )

    def __init__(self, *, font_path: str | None, image_font: Any) -> None:
        self._font_path = font_path
        self._image_font = image_font
        self._fonts: dict[int, Any] = {}
        self._base_frames: dict[tuple[int, int, tuple[Any, ...]], Any] = {}
        self._base_frame_sizes: dict[tuple[int, int, tuple[Any, ...]], int] = {}
        self._base_frame_bytes = 0
        self._base_png: dict[tuple[int, int, tuple[Any, ...]], bytes] = {}
        self._base_png_bytes = 0
        self._max_cache_bytes = 16 * 1024 * 1024

    def font(self, size: int) -> Any:
        if self._image_font is None:
            return None
        size = max(int(size), 8)
        font = self._fonts.pop(size, None)
        if font is not None:
            self._fonts[size] = font
            return font
        paths = ([self._font_path] if self._font_path else []) + list(self._FONT_PATHS)
        for candidate in paths:
            if candidate and Path(candidate).is_file():
                try:
                    font = self._image_font.truetype(candidate, size)
                    self._fonts[size] = font
                    return font
                except OSError:
                    pass
        font = self._image_font.load_default()
        self._fonts[size] = font
        if len(self._fonts) > 32:
            self._fonts.pop(next(iter(self._fonts)))
        return font

    def trim(self, protected_key: tuple[Any, ...] | None = None) -> None:
        while (
            len(self._base_frames) > 8
            or len(self._base_png) > 8
            or self._base_frame_bytes + self._base_png_bytes > self._max_cache_bytes
        ):
            frame_key = next((key for key in self._base_frames if key != protected_key), None)
            png_key = next((key for key in self._base_png if key != protected_key), None)
            if frame_key is None and png_key is None:
                break
            if len(self._base_frames) > 8 or (
                self._base_frame_bytes >= self._base_png_bytes and frame_key is not None
            ):
                assert frame_key is not None
                self._base_frames.pop(frame_key, None)
                self._base_frame_bytes -= self._base_frame_sizes.pop(frame_key, 0)
            else:
                assert png_key is not None
                png = self._base_png.pop(png_key, None)
                if png is not None:
                    self._base_png_bytes -= len(png)

    def base_image(
        self,
        key: tuple[int, int, tuple[Any, ...]],
        render: Callable[[], Any],
    ) -> Any:
        base = self._base_frames.pop(key, None)
        if base is not None:
            self._base_frames[key] = base
            return base
        base = render()
        self._base_frames[key] = base
        self._base_frame_sizes[key] = key[0] * key[1] * 4
        self._base_frame_bytes += self._base_frame_sizes[key]
        self.trim(protected_key=key)
        return base

    def cached_png(self, key: tuple[int, int, tuple[Any, ...]]) -> bytes | None:
        value = self._base_png.pop(key, None)
        if value is not None:
            self._base_png[key] = value
        return value

    def save_png(self, key: tuple[int, int, tuple[Any, ...]], value: bytes) -> None:
        self._base_png[key] = value
        self._base_png_bytes += len(value)
        self.trim(protected_key=key)


__all__ = ["FramebufferCache"]
