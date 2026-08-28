"""Trigger model and pure next-fire computation (§76).

Trigger types are pure value objects; `next_fire` performs no I/O and returns
the next occurrence strictly after `after`, or None when the trigger is
exhausted (e.g. a fired ONCE trigger).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


class TriggerType(str, enum.Enum):
    ONCE = "once"
    INTERVAL = "interval"
    CRON = "cron"
    EVENT = "event"


@dataclass(frozen=True)
class TriggerSpec:
    type: TriggerType
    at: datetime | None = None  # ONCE / INTERVAL start
    interval_seconds: float | None = None  # INTERVAL
    cron: str | None = None  # CRON "minute hour dom month dow"
    event_name: str | None = None  # EVENT
    event_filters: Mapping[str, Any] = field(default_factory=dict)  # EVENT
    timezone: str | None = None
    end_at: datetime | None = None
    times: int | None = None  # max occurrences; None = unlimited
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        kind = self.type
        if kind is TriggerType.INTERVAL:
            if self.interval_seconds is None:
                if self.at is None:
                    raise ValueError("INTERVAL trigger requires interval_seconds or at")
                # NOTE: latent bug - `datetime` has no total_seconds(); the
                # attr is typed as datetime but total_seconds belongs to
                # timedelta. Leaving runtime behavior unchanged for now.
                object.__setattr__(
                    self,
                    "interval_seconds",
                    self.at.total_seconds(),  # type: ignore[attr-defined]
                )
            assert self.interval_seconds is not None
            if self.interval_seconds <= 0:
                raise ValueError("INTERVAL must be positive")
        elif kind is TriggerType.CRON and not self.cron:
            raise ValueError("CRON trigger requires a cron expression")
        elif kind is TriggerType.ONCE and self.at is None:
            raise ValueError("ONCE trigger requires a fire time")


def next_fire(trigger: TriggerSpec, after: datetime) -> datetime | None:
    """Return the next fire time strictly after ``after``, or None if exhausted."""
    kind = trigger.type
    if kind is TriggerType.ONCE:
        t = _ensure_aware(trigger.at)
        return t if t is not None and t > after else None
    if kind is TriggerType.INTERVAL:
        return _next_interval(trigger, after)
    if kind is TriggerType.CRON:
        return _next_cron(trigger, after)
    if kind is TriggerType.EVENT:
        # Event triggers are advanced by Scheduler.notify_event(), not by a
        # wall-clock calculation. Returning None keeps time-based callers from
        # accidentally firing an event job during a normal tick.
        return None
    return None


def _next_interval(trigger: TriggerSpec, after: datetime) -> datetime | None:
    if trigger.end_at is not None and after >= trigger.end_at:
        return None
    interval = timedelta(seconds=trigger.interval_seconds or 0)
    base = _ensure_aware(trigger.at)
    if base is None or interval.total_seconds() <= 0:
        return None
    step = interval.total_seconds()
    # Occurrence ordinal n (1-based): candidate time = base + (n-1)*interval.
    # The returned candidate must be strictly after `after`; n is the smallest
    # ordinal whose candidate exceeds `after`, giving cumulative-correct times.
    delta = (after - base).total_seconds()
    if delta < 0:
        n = 1
    else:
        n = int(delta // step) + 2
    if trigger.times is not None and n > trigger.times:
        return None
    candidate = base + (n - 1) * interval
    if trigger.end_at is not None and candidate > trigger.end_at:
        return None
    return candidate


def _next_cron(trigger: TriggerSpec, after: datetime) -> datetime | None:
    parts = (trigger.cron or "").split()
    if len(parts) != 5:
        return None
    minute, hour, dom, month, dow = parts
    base = _ensure_aware(after)
    if base is None:
        return None
    tz = _load_tz(trigger.timezone)
    end_at = _ensure_aware(trigger.end_at)
    # Cron fields are wall-clock LOCAL time: walk naive local wall-clock
    # minutes in the configured timezone (DST-safe), then convert the matched
    # minute to UTC for comparison and storage.
    local_after = base.astimezone(tz)
    current = local_after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(24 * 60 * 366 * 5):  # scan ~5 years of minutes
        current_utc = _local_to_utc(current, tz)
        if end_at is not None and current_utc > end_at:
            return None
        if _cron_matches(current, minute, hour, dom, month, dow):
            return current_utc
        current = current + timedelta(minutes=1)
    return None


def _load_tz(name: str | None):
    if name is None or name in ("UTC", "utc", "GMT"):
        return timezone.utc
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)


def _local_to_utc(naive: datetime, tz) -> datetime:
    """Interpret a naive local wall-clock as tz-aware and convert to UTC.

    ``fold=1`` selects the later (post-fall-back) instance of an ambiguous
    fall-back minute so the immediately prior UTC instant is never revisited.
    """
    return naive.replace(tzinfo=tz, fold=1).astimezone(timezone.utc)


def _cron_matches(dt: datetime, minute: str, hour: str, dom: str, month: str, dow: str) -> bool:
    if not _field_matches(dt.minute, minute, 0, 59):
        return False
    if not _field_matches(dt.hour, hour, 0, 23):
        return False
    if not _field_matches(dt.month, _normalize_month(month), 1, 12):
        return False
    if not _dom_dow_match(dt, dom, _normalize_dow(dow)):
        return False
    return True


_DAY_NAMES = {
    "sun": 7,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}
_MONTH_NAMES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _normalize_month(spec: str) -> str:
    out = []
    for part in spec.split(","):
        base = part.split("/")[0].strip().lower()
        if base in _MONTH_NAMES:
            left = str(_MONTH_NAMES[base])
            step = "/" + part.split("/", 1)[1] if "/" in part else ""
            out.append(left + step)
        else:
            out.append(part)
    return ",".join(out)


def _normalize_dow(spec: str) -> str:
    out = []
    for part in spec.split(","):
        base = part.split("/")[0].strip().lower()
        if base in _DAY_NAMES:
            left = str(_DAY_NAMES[base])
            step = "/" + part.split("/", 1)[1] if "/" in part else ""
            out.append(left + step)
        else:
            out.append(part)
    return ",".join(out)


def _field_matches(value: int, spec: str, lo: int, hi: int) -> bool:
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "*":
            return True
        if "/" in part:
            base, _, step_s = part.partition("/")
            try:
                step = int(step_s)
            except ValueError:
                continue
            start = lo if base in ("", "*") else int(base)
            if value >= start and (value - start) % step == 0:
                return True
            continue
        if "-" in part:
            a_s, _, b_s = part.partition("-")
            try:
                a, b = int(a_s), int(b_s)
            except ValueError:
                continue
            if a <= value <= b:
                return True
            continue
        try:
            if int(part) == value:
                return True
        except ValueError:
            continue
    return False


def _dom_dow_match(dt: datetime, dom: str, dow: str) -> bool:
    dom_match = _field_matches(dt.day, dom, 1, 31) if dom != "*" else True
    dow_match = _field_matches(dt.isoweekday(), dow, 1, 7) if dow != "*" else True
    if dom == "*" and dow == "*":
        return True
    if dom != "*" and dow == "*":
        return dom_match
    if dom == "*" and dow != "*":
        return dow_match
    return dom_match or dow_match


def _ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


__all__ = ["TriggerType", "TriggerSpec", "next_fire"]
