"""Protocol-neutral autonomy decoding shared across dispatch boundaries."""

from __future__ import annotations

from typing import Mapping

from athena.protocol.tasks import AutonomyLevel


__all__ = ["InvalidTaskAutonomyError", "resolve_autonomy_value", "resolve_task_autonomy"]


class InvalidTaskAutonomyError(ValueError):
    """A persisted autonomy value is present but not protocol-valid."""


def resolve_task_autonomy(metadata: Mapping[str, object] | None) -> AutonomyLevel | None:
    """Return explicit task autonomy, or ``None`` only when genuinely omitted."""
    return resolve_autonomy_value((metadata or {}).get("autonomy"))


def resolve_autonomy_value(raw: object) -> AutonomyLevel | None:
    """Decode an explicit autonomy value without inventing a default."""
    if raw is None:
        return None
    if isinstance(raw, AutonomyLevel):
        return raw
    try:
        return AutonomyLevel(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidTaskAutonomyError(f"invalid task autonomy: {raw!r}") from exc
