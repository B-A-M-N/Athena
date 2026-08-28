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
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Mapping

from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow

from athena.protocol.tasks import (
    CapabilityPolicy,
    Criterion,
    DeliverySpec,
    MutationMode,
    ModelPolicy,
    NetworkPolicy,
    ResourceBudget,
    TaskSpec,
    VerificationSpec,
    VerificationType,
    WorkspaceSpec,
)
from athena.scheduler.claims import Claim, _to_claim, claim_next
from athena.scheduler.triggers import TriggerType, TriggerSpec, next_fire
from athena.state.schedules import ScheduleStore

_logger = logging.getLogger("athena.scheduler")


@dataclass(frozen=True)
class TaskTemplate:
    objective: str
    session_id: str | None = None
    parent_task_id: str | None = None
    workspace_id: str | None = None
    workspace_root: str | None = None
    network_policy: str | None = None
    mutation_mode: str | None = None
    capability_allow: tuple[str, ...] = ()
    model_role: str = "primary"
    max_agent_iterations: int | None = None
    deadline: datetime | None = None
    delivery_channel: str | None = None
    acceptance_criteria: tuple[Criterion, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def build_task_spec(self, job_id: str, occurrence_key: str | None = None) -> TaskSpec:
        workspace = None
        if self.workspace_id or self.workspace_root:
            workspace_kwargs: dict[str, Any] = {}
            if self.network_policy:
                workspace_kwargs["network_policy"] = NetworkPolicy(self.network_policy)
            if self.mutation_mode:
                workspace_kwargs["mutation_mode"] = MutationMode(self.mutation_mode)
            workspace = WorkspaceSpec(
                id=self.workspace_id or job_id,
                root=self.workspace_root or ".",
                **workspace_kwargs,
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
            acceptance_criteria=self.acceptance_criteria,
            session_id=self.session_id,
            parent_task_id=self.parent_task_id,
            workspace=workspace,
            capability_policy=CapabilityPolicy(allow=self.capability_allow),
            model_policy=ModelPolicy(role=self.model_role),
            resource_budget=budget,
            deadline=self.deadline,
            delivery=(
                None if not self.delivery_channel else DeliverySpec(channel=self.delivery_channel)
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
    src = payload if isinstance(payload, dict) else (meta if isinstance(meta, dict) else {})
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
        network_policy=active.get("network_policy"),
        mutation_mode=active.get("mutation_mode"),
        capability_allow=tuple(active.get("capability_allow") or ()),
        model_role=active.get("model_role", "primary"),
        max_agent_iterations=active.get("max_agent_iterations"),
        deadline=deadline,
        delivery_channel=active.get("delivery_channel"),
        acceptance_criteria=_criteria_from_records(active.get("acceptance_criteria")),
        metadata=dict(active.get("metadata") or {}),
    )


def _criteria_from_records(value: Any) -> tuple[Criterion, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    criteria: list[Criterion] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        raw = item.get("verification")
        verification = None
        if isinstance(raw, Mapping):
            try:
                verification = VerificationSpec(
                    type=VerificationType(str(raw.get("type") or "manual")),
                    command=raw.get("command"),
                    path=raw.get("path"),
                    predicate=raw.get("predicate"),
                    capability=raw.get("capability"),
                )
            except ValueError:
                continue
        criteria.append(
            Criterion(
                id=str(item.get("id") or ""),
                description=str(item.get("description") or ""),
                verification=verification,
                required=bool(item.get("required", True)),
            )
        )
    return tuple(criteria)


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
            await self._fire_claim(job, claim)
            fires += 1
        return fires

    async def notify_event(self, event: Any) -> int:
        """Fire matching EVENT jobs using the same durable claim path.

        The event ID is the occurrence identity. Replayed or multiply-delivered
        events therefore produce at most one task per job, even across a
        scheduler restart.
        """
        event_type = str(getattr(event, "type", ""))
        payload = dict(getattr(event, "payload", {}) or {})
        event_id = str(getattr(event, "id", "") or "")
        if not event_id:
            return 0
        if getattr(event, "task_id", None) is not None:
            payload.setdefault("task_id", event.task_id)
        if getattr(event, "session_id", None) is not None:
            payload.setdefault("session_id", event.session_id)

        fired = 0
        for job in await self._store.list_jobs(enabled_only=True):
            trigger = _trigger_from_job(job)
            if trigger is None or trigger.type is not TriggerType.EVENT:
                continue
            if trigger.event_name and trigger.event_name != event_type:
                continue
            if not _filters_match(trigger.event_filters, payload):
                continue
            if trigger.end_at is not None and utcnow() > trigger.end_at:
                await self._store.set_enabled(job["id"], False)
                continue
            if trigger.times is not None:
                count = await self._store.count_runs(job["id"])
                if count >= trigger.times:
                    await self._store.set_enabled(job["id"], False)
                    continue
            scheduled_for = f"event:{event_id}"
            claim = await self._store.claim_next_due(job["id"], scheduled_for)
            if claim is None:
                continue
            await self._fire_claim(job, _to_claim(claim), event=event)
            fired += 1
        return fired

    async def _fire_claim(self, job: dict, claim: Claim, *, event: Any = None) -> None:
        template = _template_from_job(job)
        occurrence_key = f"{claim.job_id}|{claim.scheduled_for}"
        metadata = dict(template.metadata)
        if event is not None:
            metadata["_trigger_event"] = {
                "id": getattr(event, "id", None),
                "type": getattr(event, "type", None),
                "payload": dict(getattr(event, "payload", {}) or {}),
            }
        template = replace(template, metadata=metadata)
        spec = template.build_task_spec(job["id"], occurrence_key=occurrence_key)
        try:
            created = await self._tm.create(spec)
        except Exception:
            await self._store.release_claim(claim.claim_id, job["id"], claim.scheduled_for)
            raise
        task_id = created.id if created is not None else None
        if task_id is not None:
            await self._tm.enqueue(task_id)
        trigger = _trigger_from_job(job)
        disable = bool(
            trigger is not None
            and trigger.times is not None
            and await self._store.count_runs(job["id"]) >= trigger.times
        )
        next_run, time_disable = self._next_run(job, claim)
        await self._store.complete_claim(
            claim.claim_id,
            job["id"],
            task_id,
            next_run=next_run,
            disable=disable or time_disable,
        )

    def _next_run(self, job: dict, claim) -> tuple[str | None, bool]:
        """Compute the next occurrence and whether the job is exhausted.

        Pure decision (no I/O) so it can be persisted atomically with the fired
        marker; a crash between firing and advancing cannot leave the job wedged
        on an already-fired occurrence (§77, §86).
        """
        trigger = _trigger_from_job(job)
        if trigger is None:
            return None, False
        if trigger.type is TriggerType.EVENT:
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

    def is_running(self) -> bool:
        """True iff the background tick loop task exists and is not done.

        Public readiness predicate (health checks read this instead of poking
        ``_task``). Same semantics as the loop-guard in :meth:`start`: a
        scheduler that was never started, has been stopped, or whose loop
        task has already completed reports ``False``.
        """
        return self._task is not None and not (
            self._task.done() if hasattr(self._task, "done") else True
        )

    async def _run(self) -> None:
        # Give callers one scheduling turn after startup to finish durable
        # setup or perform an explicit tick.  Immediate first-pass polling
        # makes a due occurrence race with recovery/bootstrap code.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self._loop_interval)
        except asyncio.TimeoutError:
            pass
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


def _filters_match(filters: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    """Match scalar event filters exactly; nested mappings use equality."""
    for key, expected in dict(filters or {}).items():
        if payload.get(key) != expected:
            return False
    return True


__all__ = ["ScheduledJob", "Scheduler", "TaskTemplate", "TriggerSpec"]
