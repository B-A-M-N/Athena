"""Scheduler engine (§74-77).

The scheduler is a trigger + claim engine ONLY. It does NOT run an agent loop
and does not invoke an LLM. On each tick it:

    1. atomically claims due job occurrences (idempotent via §77 unique index),
    2. instantiates a TaskSpec from the job's template,
    3. enqueues that Task via TaskManager.create(spec),
    4. records the run as fired and reschedules the next fire per its trigger.

The TaskManager/Worker/kernel executes the resulting Task.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from athena.protocol.tasks import (
    CapabilityPolicy,
    DeliverySpec,
    ModelPolicy,
    ResourceBudget,
    TaskSpec,
    WorkspaceSpec,
)
from athena.scheduler.claims import claim_next
from athena.scheduler.triggers import TriggerType, TriggerSpec, next_fire
from athena.state.schedules import ScheduleStore


@dataclass(frozen=True)
class TaskTemplate:
    objective: str
    session_id: str | None = None
    parent_task_id: str | None = None
    workspace_id: str | None = None
    workspace_root: str | None = None
    capability_allow: tuple[str, ...] = ()
    model_role: str = "primary"
    max_agent_iterations: int | None = None
    deadline: datetime | None = None
    delivery_channel: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def build_task_spec(
        self, job_id: str, occurrence_key: str | None = None
    ) -> TaskSpec:
        workspace = None
        if self.workspace_id or self.workspace_root:
            workspace = WorkspaceSpec(
                id=self.workspace_id or job_id,
                root=self.workspace_root or ".",
            )
        budget = ResourceBudget()
        if self.max_agent_iterations is not None:
            budget = ResourceBudget(max_agent_iterations=self.max_agent_iterations)
        metadata = dict(self.metadata)
        if occurrence_key is not None:
            metadata["_occurrence"] = occurrence_key
        return TaskSpec(
            id=new_id("task"),
            objective=self.objective,
            session_id=self.session_id,
            parent_task_id=self.parent_task_id,
            workspace=workspace,
            capability_policy=CapabilityPolicy(allow=self.capability_allow),
            model_policy=ModelPolicy(role=self.model_role),
            resource_budget=budget,
            deadline=self.deadline,
            delivery=(
                None
                if not self.delivery_channel
                else DeliverySpec(channel=self.delivery_channel)
            ),
            metadata=metadata,
        )


@dataclass(frozen=True)
class ScheduledJob:
    id: str
    name: str
    trigger: TriggerSpec
    task_template: TaskTemplate
    timezone: str = "UTC"
    enabled: bool = True
    next_fire: datetime | None = None
    last_run: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


def trigger_to_spec(trigger: TriggerSpec) -> dict[str, Any]:
    return {
        "type": trigger.type.value,
        "at": trigger.at.isoformat() if trigger.at else None,
        "interval_seconds": trigger.interval_seconds,
        "cron": trigger.cron,
        "event_name": trigger.event_name,
        "event_filters": dict(trigger.event_filters),
        "timezone": trigger.timezone,
        "end_at": trigger.end_at.isoformat() if trigger.end_at else None,
        "times": trigger.times,
        "metadata": dict(trigger.metadata),
    }


def trigger_from_dict(data: Mapping[str, Any]) -> TriggerSpec:
    at = _to_dt(data.get("at"))
    end_at = _to_dt(data.get("end_at"))
    return TriggerSpec(
        type=TriggerType(data.get("type") or "interval"),
        at=at,
        interval_seconds=data.get("interval_seconds"),
        cron=data.get("cron"),
        event_name=data.get("event_name"),
        event_filters=dict(data.get("event_filters") or {}),
        timezone=data.get("timezone"),
        end_at=end_at,
        times=data.get("times"),
        metadata=dict(data.get("metadata") or {}),
    )


def _to_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _trigger_from_job(job: dict) -> TriggerSpec | None:
    meta = job.get("metadata")
    if isinstance(meta, dict) and meta.get("_trigger_spec"):
        return trigger_from_dict(meta["_trigger_spec"])
    payload = job.get("payload")
    if isinstance(payload, dict) and payload.get("trigger"):
        return trigger_from_dict(payload["trigger"])
    if isinstance(meta, dict) and meta.get("trigger"):
        return trigger_from_dict(meta["trigger"])
    return None


def _template_from_job(job: dict) -> TaskTemplate:
    payload = job.get("payload")
    meta = job.get("metadata")
    src = (
        payload
        if isinstance(payload, dict)
        else (meta if isinstance(meta, dict) else {})
    )
    raw_template = src.get("template")
    template = raw_template if isinstance(raw_template, dict) else src
    itinerary = template.get("task_template")
    active = itinerary if isinstance(itinerary, dict) else template
    deadline = _to_dt(active.get("deadline"))
    return TaskTemplate(
        objective=active.get("objective") or job.get("name") or "",
        session_id=active.get("session_id"),
        parent_task_id=active.get("parent_task_id"),
        workspace_id=active.get("workspace_id"),
        workspace_root=active.get("workspace_root"),
        capability_allow=tuple(active.get("capability_allow") or ()),
        model_role=active.get("model_role", "primary"),
        max_agent_iterations=active.get("max_agent_iterations"),
        deadline=deadline,
        delivery_channel=active.get("delivery_channel"),
        metadata=dict(active.get("metadata") or {}),
    )


class Scheduler:
    """Trigger + claim engine. Creates Tasks only; never runs an agent loop."""

    def __init__(
        self,
        store: ScheduleStore,
        task_manager: Any,
        *,
        max_concurrent: int = 0,
        loop_interval_seconds: float = 1.0,
    ) -> None:
        self._store = store
        self._tm = task_manager
        self._max_concurrent = max_concurrent
        self._loop_interval = loop_interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def tick(self, now: datetime | None = None) -> int:
        """Claim and enqueue due jobs for this tick. Returns number fired."""
        now = now or utcnow()
        fires = 0
        while True:
            if self._max_concurrent and fires >= self._max_concurrent:
                break
            claim = await claim_next(self._store, now)
            if claim is None:
                break
            job = await self._store.get_job_id(claim.job_id)
            if job is None:
                break
            template = _template_from_job(job)
            occurrence_key = f"{claim.job_id}|{claim.scheduled_for}"
            spec = template.build_task_spec(job["id"], occurrence_key=occurrence_key)
            try:
                created = await self._tm.create(spec)
            except Exception:
                await self._store.release_claim(
                    claim.claim_id, job["id"], claim.scheduled_for
                )
                raise
            task_id = created.id if created is not None else None
            if task_id is not None:
                await self._tm.enqueue(task_id)
            next_run, disable = self._next_run(job, claim)
            await self._store.complete_claim(
                claim.claim_id,
                job["id"],
                task_id,
                next_run=next_run,
                disable=disable,
            )
            fires += 1
        return fires

    def _next_run(self, job: dict, claim) -> tuple[str | None, bool]:
        """Compute the next occurrence and whether the job is exhausted.

        Pure decision (no I/O) so it can be persisted atomically with the fired
        marker; a crash between firing and advancing cannot leave the job wedged
        on an already-fired occurrence (§77, §86).
        """
        trigger = _trigger_from_job(job)
        if trigger is None:
            return None, False
        scheduled_for = _to_dt(claim.scheduled_for) or utcnow()
        nxt = next_fire(trigger, scheduled_for)
        if nxt is None:
            if trigger.type is TriggerType.ONCE or (
                trigger.type is TriggerType.INTERVAL and trigger.times is not None
            ):
                return None, True
            return None, False
        return nxt.isoformat(), False

    async def start(self) -> None:
        """Start the background tick loop."""
        if self._task is not None and not self._task.done():
            return
        await self.reconcile()
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def reconcile(self) -> None:
        """Recover orphaned CLAIMED occurrences left by a crash mid-fire.

        A crash between Task creation and ``complete_claim`` leaves a CLAIMED
        occurrence with no linked task. If the created Task is found (via the
        deterministic ``(job_id, scheduled_for)`` occurrence key stamped into
        its metadata) the occurrence is marked FIRED; otherwise it is released
        so the next tick reclaims and retries it.
        """
        await self._store.reconcile_stale_occurrences()

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as exc:
                _logger.warning("scheduler tick failed: %s", exc)
                # If a claim was left open, attempt immediate reconciliation
                try:
                    await self.reconcile()
                except Exception:
                    pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._loop_interval)
            except asyncio.TimeoutError:
                continue


__all__ = ["ScheduledJob", "Scheduler", "TaskTemplate", "TriggerSpec"]
