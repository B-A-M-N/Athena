"""Bounded tool-input correction accounting for kernel dispatch.

This mechanism only classifies canonical capability results and records the
per-capability correction count. It does not dispatch, repair, escalate, or
finalize; the kernel remains the sole next-action authority.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from athena.protocol.messages import CapabilityResultBlock

__all__ = ["ToolInputCorrectionMechanism"]


class ToolInputCorrectionMechanism:
    """Track invalid tool-input corrections within the declared cycle budget."""

    @staticmethod
    def exhausted_capabilities(
        state: Any, results: Sequence[CapabilityResultBlock], *, max_cycles: int
    ) -> tuple[str, ...]:
        exhausted: list[str] = []
        for result in results:
            if not isinstance(result, CapabilityResultBlock):
                continue
            if not (result.error or "").startswith("tool_input_invalid"):
                continue
            count = state.tool_correction_counts.get(result.capability_id, 0) + 1
            state.tool_correction_counts[result.capability_id] = count
            if count > max_cycles:
                exhausted.append(result.capability_id)
        return tuple(sorted(set(exhausted)))
