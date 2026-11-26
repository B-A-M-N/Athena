"""Shared result construction for synthesis operation handlers."""

from __future__ import annotations

from typing import Any

from athena.protocol.capabilities import CapabilityResult, CapabilityResultStatus

__all__ = ["synthesis_result"]


def synthesis_result(
    request: Any,
    *,
    ok: bool = True,
    output: str = "",
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> CapabilityResult:
    """Build the canonical result shape for a synthesis operation."""
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
        metadata=dict(metadata or {}),
    )
