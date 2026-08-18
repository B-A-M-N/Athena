"""Atomic claim logic for due scheduled occurrences (§77).

Idempotency is enforced by the persistence layer's unique index on
``(job_id, scheduled_for)``: ``ScheduleStore.claim_next_due`` atomically
claims one occurrence and returns None for an already-claimed occurrence, so
concurrent or restarted workers can never double-execute the same fire.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from athena.state.schedules import ScheduleStore


@dataclass(frozen=True)
class Claim:
    claim_id: str
    job_id: str
    scheduled_for: str
    claim_started_at: str | None = None
    raw: dict[str, Any] | None = None


async def claim_next(store: ScheduleStore, now: datetime) -> Claim | None:
    """Atomically claim the first due, unlocked job occurrence, or None."""
    jobs = await store.list_jobs(enabled_only=True)
    iso_now = now.isoformat()
    for job in jobs:
        next_run = job.get("next_run")
        if not next_run or next_run > iso_now:
            continue
        claim = await store.claim_next_due(job["id"], next_run)
        if claim is not None:
            return _to_claim(claim)
    return None


def _to_claim(claim: dict) -> Claim:
    return Claim(
        claim_id=claim["claim_id"],
        job_id=claim["job_id"],
        scheduled_for=claim["scheduled_for"],
        raw=claim,
    )


__all__ = ["Claim", "claim_next"]
