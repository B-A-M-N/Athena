"""ToolCallCandidate boundary: preserves RAW malformed tool arguments.

When a provider sends tool-call arguments that fail to parse as a JSON
object, this module keeps the EXACT raw string alive (keyed by call_id) so the
repair engine can act on the original bytes (double-decode, control-char
escaping, ...) instead of an already-collapsed empty dict.  The candidate
type itself lives in the provider-neutral protocol module; this module keeps
the compatibility lookup used by older callers.
"""

from __future__ import annotations

from athena.protocol.models import ToolCallCandidate

__all__ = ["ToolCallCandidate", "record_raw_candidate", "get_raw_candidate", "clear_raw_candidates"]


# -- process-local registry keyed by call_id --------------------------------

_REGISTRY: dict[str, ToolCallCandidate] = {}


def record_raw_candidate(candidate: ToolCallCandidate) -> None:
    """Record a raw candidate for later lookup by the repair path."""
    _REGISTRY[candidate.call_id] = candidate


def get_raw_candidate(call_id: str) -> ToolCallCandidate | None:
    return _REGISTRY.get(call_id)


def clear_raw_candidates() -> None:
    _REGISTRY.clear()
