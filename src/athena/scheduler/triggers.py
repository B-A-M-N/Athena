"""Compatibility exports for the neutral scheduling protocol."""

from athena.protocol.scheduling import (
    TriggerSpec,
    TriggerType,
    load_timezone,
    local_utc_candidates,
    next_fire,
)

_load_tz = load_timezone
_local_utc_candidates = local_utc_candidates

__all__ = ["TriggerType", "TriggerSpec", "next_fire"]
