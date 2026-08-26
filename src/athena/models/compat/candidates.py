"""ToolCallCandidate boundary: preserves RAW malformed tool arguments.

When a provider sends tool-call arguments that fail to parse as a JSON
object, the canonical CapabilityCallBlock collapses them to ``{}`` so the
rest of the pipeline stays type-safe. This module is the boundary that
keeps the EXACT raw string alive (keyed by call_id) so the repair engine
can act on the original bytes (double-decode, control-char escaping, ...)
instead of the already-collapsed empty dict.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

__all__ = ["ToolCallCandidate", "record_raw_candidate", "get_raw_candidate",
           "clear_raw_candidates"]


@dataclass
class ToolCallCandidate:
    """Raw provider tool-call boundary before canonicalization."""

    call_id: str
    capability_id: str
    raw_arguments: str
    parsed_arguments: dict | None = None
    completion_state: str = "CLEAN"   # CLEAN | INTERRUPTED | UNKNOWN
    provider_profile_id: str | None = None
    model_id: str | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(
        cls,
        call_id: str,
        capability_id: str,
        raw: str,
        *,
        completion_state: str = "CLEAN",
        provider_profile_id: str | None = None,
        model_id: str | None = None,
        **metadata: Any,
    ) -> "ToolCallCandidate":
        """Build a candidate. ``parsed_arguments`` is set ONLY when
        ``json.loads`` succeeds AND yields a dict — never manufactured {}."""
        parsed: dict | None = None
        try:
            loaded = json.loads(raw)
        except (ValueError, TypeError):
            loaded = None
        if isinstance(loaded, dict):
            parsed = loaded
        return cls(
            call_id=call_id,
            capability_id=capability_id,
            raw_arguments=raw,
            parsed_arguments=parsed,
            completion_state=completion_state,
            provider_profile_id=provider_profile_id,
            model_id=model_id,
            provider_metadata=dict(metadata),
        )


# -- process-local registry keyed by call_id --------------------------------

_REGISTRY: dict[str, ToolCallCandidate] = {}


def record_raw_candidate(candidate: ToolCallCandidate) -> None:
    """Record a raw candidate for later lookup by the repair path."""
    _REGISTRY[candidate.call_id] = candidate


def get_raw_candidate(call_id: str) -> ToolCallCandidate | None:
    return _REGISTRY.get(call_id)


def clear_raw_candidates() -> None:
    _REGISTRY.clear()
