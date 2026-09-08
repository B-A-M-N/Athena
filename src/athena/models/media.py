"""Provider-bound media hydration for durable local artifacts.

The canonical transcript stores an ``artifact://`` reference, never the image
bytes. Providers that accept image inputs need a request-local data URL, so
this module resolves only task-produced immutable artifact references at the
adapter boundary. A missing or oversized local blob remains an explicit text
reference rather than becoming fabricated visual input.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from athena.protocol.messages import ArtifactRefBlock, ImageBlock

_MAX_INLINE_IMAGE_BYTES = 4 * 1024 * 1024


def image_data_path(block: Any) -> str | None:
    """Return a provider-usable URL/data URL for one canonical image block."""
    if isinstance(block, ImageBlock):
        value = str(block.data_path or "")
        return value or None
    if not isinstance(block, ArtifactRefBlock):
        return None
    ref = block.ref
    mime = str(getattr(ref, "mime_type", None) or "")
    if not mime.startswith("image/"):
        return None
    uri = str(block.uri or "")
    if uri.startswith(("https://", "http://", "data:")):
        return uri
    path = str(getattr(ref, "storage_path", None) or "")
    if not path:
        return None
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if not data or len(data) > _MAX_INLINE_IMAGE_BYTES:
        return None
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


__all__ = ["image_data_path"]
