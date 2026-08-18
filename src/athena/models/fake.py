"""Backward-compatible shim for the canonical :class:`FakeModelProvider`.

The real implementation lives at ``athena.models.providers.fake``; this module
re-exports it so the historical ``athena.models.fake`` import path keeps
working with a single source of truth.
"""

from __future__ import annotations

from athena.models.providers.fake import FakeModelProvider

__all__ = ["FakeModelProvider"]