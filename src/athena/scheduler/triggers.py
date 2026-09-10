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
                raise ValueError("INTERVAL trigger requires interval_seconds")
            if isinstance(self.interval_seconds, bool) or not isinstance(
                self.interval_seconds, (int, float)
            ):
                raise ValueError("INTERVAL interval_seconds must be numeric")
            if not (self.interval_seconds > 0):
                raise ValueError("INTERVAL must be positive")
        elif kind is TriggerType.CRON:
            if not self.cron:
                raise ValueError("CRON trigger requires a cron expression")
            _validate_cron(self.cron)
        elif kind is TriggerType.ONCE and self.at is None:
            raise ValueError("ONCE trigger requires a fire time")
        if self.times is not None and (
            isinstance(self.times, bool) or not isinstance(self.times, int) or self.times < 1
        ):
            raise ValueError("trigger.times must be a positive integer")
        if self.timezone not in (None, "", "UTC", "utc", "GMT"):
            try:
                _load_tz(self.timezone)
            except Exception as exc:
                raise ValueError(f"invalid trigger timezone: {self.timezone}") from exc


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
        raise ValueError("CRON trigger requires exactly five fields")
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
        if _cron_matches(current, minute, hour, dom, month, dow):
            # A fall-back minute has two real instants. Choose the first one
            # after ``after`` and, when that has passed, the second. A
            # spring-forward phantom minute has no valid candidate.
            candidates = [
                candidate for candidate in _local_utc_candidates(current, tz) if candidate > base
            ]
            if candidates:
                current_utc = min(candidates)
                if end_at is not None and current_utc > end_at:
                    return None
                return current_utc
        current = current + timedelta(minutes=1)
    return None


def _load_tz(name: str | None):
    if name is None or name in ("UTC", "utc", "GMT"):
        return timezone.utc
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)


def _local_to_utc(naive: datetime, tz) -> datetime:
    """Return the earliest real UTC instant for a local wall-clock minute."""
    candidates = _local_utc_candidates(naive, tz)
    return min(candidates) if candidates else naive.replace(tzinfo=tz).astimezone(timezone.utc)


def _local_utc_candidates(naive: datetime, tz) -> tuple[datetime, ...]:
    """Resolve both DST folds, filtering nonexistent wall-clock minutes."""
    # Callers walk local time with an aware ``datetime`` so that arithmetic
    # remains attached to the configured zone.  Fold resolution, however,
    # must compare the resulting wall clock against a naive wall-clock value;
    # retaining the input tzinfo makes every valid candidate look mismatched.
    naive = naive.replace(tzinfo=None)
    candidates: set[datetime] = set()
    for fold in (0, 1):
        aware = naive.replace(tzinfo=tz, fold=fold)
        if aware.astimezone(tz).replace(tzinfo=None) == naive:
            candidates.add(aware.astimezone(timezone.utc))
    return tuple(sorted(candidates))


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
    return _normalize_named_field(spec, _MONTH_NAMES)


def _normalize_dow(spec: str) -> str:
    return _normalize_named_field(spec, _DAY_NAMES)


def _normalize_named_field(spec: str, names: Mapping[str, int]) -> str:
    out: list[str] = []
    for part in spec.split(","):
        base, separator, step = part.partition("/")
        endpoints = base.split("-")
        normalized = []
        for endpoint in endpoints:
            normalized.append(str(names.get(endpoint.strip().lower(), endpoint.strip())))
        out.append("-".join(normalized) + (f"/{step}" if separator else ""))
    return ",".join(out)


def _validate_cron(expression: str) -> None:
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError("CRON trigger requires exactly five fields")
    minute, hour, dom, month, dow = parts
    _validate_field(minute, 0, 59, {}, "minute")
    _validate_field(hour, 0, 23, {}, "hour")
    _validate_field(dom, 1, 31, {}, "day-of-month")
    _validate_field(month, 1, 12, _MONTH_NAMES, "month")
    _validate_field(dow, 1, 7, _DAY_NAMES, "day-of-week")


def _validate_field(
    spec: str,
    lo: int,
    hi: int,
    names: Mapping[str, int],
    label: str,
) -> None:
    if not spec or len(spec) > 128:
        raise ValueError(f"invalid cron {label} field")
    for raw_part in spec.split(","):
        part = raw_part.strip()
        if not part:
            raise ValueError(f"invalid empty cron {label} field")
        base, separator, step_text = part.partition("/")
        if separator:
            if "/" in step_text or not step_text.isdigit() or int(step_text) <= 0:
                raise ValueError(f"invalid cron {label} step")
        elif not step_text == "":
            raise ValueError(f"invalid cron {label} field")
        if base in ("", "*"):
            if not separator and base == "":
                raise ValueError(f"invalid cron {label} field")
            continue
        endpoints = base.split("-")
        if len(endpoints) > 2 or any(not endpoint.strip() for endpoint in endpoints):
            raise ValueError(f"invalid cron {label} range")
        values: list[int] = []
        for endpoint in endpoints:
            token = endpoint.strip().lower()
            if token in names:
                values.append(names[token])
                continue
            if not token.isdigit():
                raise ValueError(f"invalid cron {label} value")
            values.append(int(token))
        if any(value < lo or value > hi for value in values):
            raise ValueError(f"cron {label} value out of range")
        if len(values) == 2 and values[0] > values[1]:
            raise ValueError(f"reversed cron {label} range")
        if separator and int(step_text) > hi - lo + 1:
            raise ValueError(f"cron {label} step out of range")


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
                if step <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            try:
                start = lo if base in ("", "*") else int(base)
            except (TypeError, ValueError):
                continue
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
