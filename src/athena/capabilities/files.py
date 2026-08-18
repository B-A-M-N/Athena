"""Backward-compatible ``files`` alias for the canonical ``fs`` capability.

The canonical implementation lives in ``capabilities.fs`` (BUILDSPEC section
9). This module re-exports it under the historical ``files`` import path and
class name so existing wiring keeps working. New code should import from
``athena.capabilities.fs``.
"""

from __future__ import annotations

from athena.capabilities.fs import (
    FilesystemCapability,
)

__all__ = ["FilesystemCapability"]