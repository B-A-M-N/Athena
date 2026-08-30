"""Capability health and circuit breaking.

This is deliberately a small runtime service rather than another planner.
The dispatcher records actual executor outcomes, and callers can inspect or
reset the resulting circuit through a normal capability.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)


@dataclass
class HealthRecord:
    capability_id: str
    status: str = "closed"
    total_calls: int = 0
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_failure: str | None = None
    last_failure_at: float | None = None
    last_success_at: float | None = None
    opened_at: float | None = None
    # Wall-clock companions are durable; monotonic values above are used only
    # for cooldown calculations in the current process.
    opened_at_wall: float | None = None
    last_failure_at_wall: float | None = None
    last_success_at_wall: float | None = None
    cooldown_seconds: float = 30.0
    _probe_in_flight: bool = field(default=False, repr=False)

    def to_record(self, now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        retry_after = 0.0
        if self.status == "open" and self.opened_at is not None:
            retry_after = max(0.0, self.cooldown_seconds - (now - self.opened_at))
        return {
            "capability_id": self.capability_id,
            "status": self.status,
            "total_calls": self.total_calls,
            "successes": self.successes,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "failure_rate": self.failures / self.total_calls if self.total_calls else 0.0,
            "last_failure": self.last_failure,
            "last_failure_at": self.last_failure_at_wall,
            "last_success_at": self.last_success_at_wall,
            "opened_at": self.opened_at_wall,
            "retry_after_seconds": round(retry_after, 3),
        }

    def persisted(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "status": self.status,
            "total_calls": self.total_calls,
            "successes": self.successes,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "last_failure": self.last_failure,
            "last_failure_at": self.last_failure_at_wall,
            "last_success_at": self.last_success_at_wall,
            "opened_at": self.opened_at_wall,
            "cooldown_seconds": self.cooldown_seconds,
        }


class CapabilityHealth:
    """In-process health registry used by the canonical dispatcher."""

    def __init__(
        self, *, failure_threshold: int = 3, cooldown_seconds: float = 30.0, store=None
    ) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(0.1, float(cooldown_seconds))
        self._records: dict[str, HealthRecord] = {}
        self._store = store
        self._dirty_calls: dict[str, int] = {}
        self._last_persisted: dict[str, float] = {}
        self._persist_batch_size = 32
        self._persist_interval = 1.0

    def _mark_dirty(self, capability_id: str) -> None:
        self._dirty_calls[capability_id] = self._dirty_calls.get(capability_id, 0) + 1

    def should_persist(self, capability_id: str, *, state_changed: bool = False) -> bool:
        """Return whether a health update crossed a durable flush boundary."""
        if state_changed:
            return True
        dirty = self._dirty_calls.get(capability_id, 0)
        last = self._last_persisted.get(capability_id)
        if last is None:
            self._last_persisted[capability_id] = time.monotonic()
            return dirty >= self._persist_batch_size
        return (
            dirty >= self._persist_batch_size or time.monotonic() - last >= self._persist_interval
        )

    def mark_persisted(self, capability_id: str) -> None:
        self._dirty_calls[capability_id] = 0
        self._last_persisted[capability_id] = time.monotonic()

    async def load(self, records: list[dict[str, Any]]) -> None:
        """Restore durable circuits before the worker can invoke tools."""
        now_wall = time.time()
        now_mono = time.monotonic()
        for value in records:
            capability_id = str(value.get("capability_id") or "")
            if not capability_id:
                continue
            cooldown = max(0.1, float(value.get("cooldown_seconds") or self.cooldown_seconds))
            opened_wall = _float_or_none(value.get("opened_at"))
            elapsed = max(0.0, now_wall - opened_wall) if opened_wall is not None else 0.0
            record = HealthRecord(
                capability_id=capability_id,
                status=str(value.get("status") or "closed"),
                total_calls=max(0, int(value.get("total_calls") or 0)),
                successes=max(0, int(value.get("successes") or 0)),
                failures=max(0, int(value.get("failures") or 0)),
                consecutive_failures=max(0, int(value.get("consecutive_failures") or 0)),
                last_failure=(
                    str(value["last_failure"]) if value.get("last_failure") is not None else None
                ),
                last_failure_at=_float_or_none(value.get("last_failure_at")),
                last_success_at=_float_or_none(value.get("last_success_at")),
                opened_at=(now_mono - elapsed if opened_wall is not None else None),
                opened_at_wall=opened_wall,
                last_failure_at_wall=_float_or_none(value.get("last_failure_at")),
                last_success_at_wall=_float_or_none(value.get("last_success_at")),
                cooldown_seconds=cooldown,
            )
            # A probe cannot safely span a process boundary.
            if record.status == "half_open":
                record.status = "open"
            self._records[capability_id] = record
            self._last_persisted[capability_id] = time.monotonic()

    async def persist(self, capability_id: str) -> None:
        if self._store is None:
            return
        record = self._records.get(capability_id)
        if record is None:
            await self._store.delete(capability_id)
        else:
            await self._store.save(record.persisted())

    async def reset_async(self, capability_id: str | None = None) -> int:
        count = self.reset(capability_id)
        if self._store is not None:
            if capability_id:
                await self._store.delete(capability_id)
            else:
                await self._store.clear()
        return count

    def before_call(self, capability_id: str) -> tuple[bool, dict[str, Any]]:
        record = self._records.setdefault(
            capability_id,
            HealthRecord(capability_id, cooldown_seconds=self.cooldown_seconds),
        )
        now = time.monotonic()
        if record.status != "open":
            return True, record.to_record(now)
        if record.opened_at is None or now - record.opened_at < record.cooldown_seconds:
            return False, record.to_record(now)
        # Permit one deterministic half-open probe. A second caller must wait
        # for its result rather than stampeding a known-dead endpoint.
        if record._probe_in_flight:
            return False, record.to_record(now)
        record.status = "half_open"
        record._probe_in_flight = True
        return True, record.to_record(now)

    def record_success(self, capability_id: str) -> dict[str, Any]:
        record = self._records.setdefault(
            capability_id,
            HealthRecord(capability_id, cooldown_seconds=self.cooldown_seconds),
        )
        self._mark_dirty(capability_id)
        record.total_calls += 1
        record.successes += 1
        record.consecutive_failures = 0
        record.last_success_at = time.monotonic()
        record.last_success_at_wall = time.time()
        record.status = "closed"
        record.opened_at = None
        record._probe_in_flight = False
        return record.to_record()

    def record_failure(self, capability_id: str, reason: str | None = None) -> dict[str, Any]:
        record = self._records.setdefault(
            capability_id,
            HealthRecord(capability_id, cooldown_seconds=self.cooldown_seconds),
        )
        self._mark_dirty(capability_id)
        record.total_calls += 1
        record.failures += 1
        record.consecutive_failures += 1
        record.last_failure = str(reason or "capability failed")[:500]
        record.last_failure_at = time.monotonic()
        record.last_failure_at_wall = time.time()
        record._probe_in_flight = False
        if record.consecutive_failures >= self.failure_threshold:
            record.status = "open"
            record.opened_at = record.last_failure_at
            record.opened_at_wall = record.last_failure_at_wall
        return record.to_record()

    def get(self, capability_id: str) -> dict[str, Any]:
        record = self._records.get(capability_id)
        return (
            record.to_record()
            if record
            else {
                "capability_id": capability_id,
                "status": "closed",
                "total_calls": 0,
                "successes": 0,
                "failures": 0,
                "consecutive_failures": 0,
                "failure_rate": 0.0,
                "last_failure": None,
                "retry_after_seconds": 0.0,
            }
        )

    def list(self) -> list[dict[str, Any]]:
        return [self._records[key].to_record() for key in sorted(self._records)]

    def reset(self, capability_id: str | None = None) -> int:
        if capability_id:
            self._records.pop(capability_id, None)
            self._dirty_calls.pop(capability_id, None)
            self._last_persisted.pop(capability_id, None)
            return 1
        count = len(self._records)
        self._records.clear()
        self._dirty_calls.clear()
        self._last_persisted.clear()
        return count


class CapabilityHealthCapability:
    descriptor = CapabilityDescriptor(
        id="capability_health",
        description=(
            "Inspect capability health and circuit-breaker state. Operations: "
            "list, inspect, reset. A failed capability is opened after repeated "
            "executor failures and receives one cooldown probe before reopening."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {"type": "string", "enum": ["list", "inspect", "reset"]},
                "capability_id": {"type": "string", "maxLength": 256},
            },
            "additionalProperties": False,
        },
        effects=frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, health: CapabilityHealth) -> None:
        self._health = health

    async def invoke(self, request: CapabilityRequest, **kwargs) -> CapabilityResult:
        del kwargs
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")
        capability_id = str(args.get("capability_id") or "")
        if operation == "list":
            import json

            return _result(request, output=json.dumps(self._health.list()))
        if operation == "inspect":
            if not capability_id:
                return _result(request, ok=False, error="inspect requires capability_id")
            import json

            return _result(request, output=json.dumps(self._health.get(capability_id)))
        if operation == "reset":
            if not capability_id:
                return _result(request, ok=False, error="reset requires capability_id")
            import json

            reset_async = getattr(self._health, "reset_async", None)
            reset = (
                await reset_async(capability_id)
                if reset_async is not None
                else self._health.reset(capability_id)
            )
            return _result(
                request,
                output=json.dumps(
                    {
                        "reset": reset,
                        "capability_id": capability_id,
                    }
                ),
            )
        return _result(request, ok=False, error=f"unknown operation: {operation}")


def _result(request, *, ok: bool = True, output: str = "", error: str | None = None):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
    )


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["CapabilityHealth", "CapabilityHealthCapability", "HealthRecord"]
