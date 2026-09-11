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
from collections import deque
from contextlib import asynccontextmanager
from contextvars import ContextVar
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
_claim_context: ContextVar[bool] = ContextVar("athena_scheduler_claim_context", default=False)


@dataclass(frozen=True)
class TaskTemplate:
    objective: str
    # ``session_id`` remains a legacy input for hand-authored templates. A
    # schedule-owned continuity session is persisted separately so changing
    # the mode to ``fresh`` never accidentally reuses it.
    session_id: str | None = None
    continuity_session_id: str | None = None
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
    delivery: DeliverySpec | None = None
    continuity: str = "fresh"
    acceptance_criteria: tuple[Criterion, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    capability_policy: CapabilityPolicy | None = None
    model_policy: ModelPolicy | None = None
    resource_budget: ResourceBudget | None = None
    autonomy: str = "supervised"
    # Service-owned, immutable-at-intake authority snapshot. The public
    # template remains descriptive; future occurrences read this separately
    # persisted snapshot so a model cannot widen its own schedule.
    authority_snapshot: Mapping[str, Any] = field(default_factory=dict)

    def build_task_spec(self, job_id: str, occurrence_key: str | None = None) -> TaskSpec:
        workspace = None
        authority_workspace = self.authority_snapshot.get("workspace")
        if not isinstance(authority_workspace, Mapping):
            authority_workspace = {}
        workspace_id = authority_workspace.get("id") or self.workspace_id
        workspace_root = authority_workspace.get("root") or self.workspace_root
        if workspace_id or workspace_root:
            workspace_kwargs: dict[str, Any] = {}
            network = authority_workspace.get("network_policy") or self.network_policy
            mutation = authority_workspace.get("mutation_mode") or self.mutation_mode
            if network:
                workspace_kwargs["network_policy"] = NetworkPolicy(str(network))
            if mutation:
                workspace_kwargs["mutation_mode"] = MutationMode(str(mutation))
            workspace = WorkspaceSpec(
                id=str(workspace_id or job_id),
                root=str(workspace_root or "."),
                readable=_path_rules(authority_workspace.get("readable")),
                writable=_path_rules(authority_workspace.get("writable")),
                temp_root=authority_workspace.get("temp_root"),
                execution_backend=authority_workspace.get("execution_backend"),
                revision=authority_workspace.get("revision"),
                **workspace_kwargs,
            )
        budget = _budget_from_record(self.authority_snapshot.get("resource_budget"))
        if budget is None:
            budget = self.resource_budget or ResourceBudget()
        if (
            self.max_agent_iterations is not None
            and self.resource_budget is None
            and not self.authority_snapshot.get("resource_budget")
        ):
            budget = ResourceBudget(max_agent_iterations=self.max_agent_iterations)
        capability_policy = (
            _capability_policy_from_record(self.authority_snapshot.get("capability_policy"))
            or self.capability_policy
        )
        if capability_policy is None:
            capability_policy = CapabilityPolicy(allow=self.capability_allow)
        model_policy = (
            _model_policy_from_record(self.authority_snapshot.get("model_policy"))
            or self.model_policy
        )
        if model_policy is None:
            model_policy = ModelPolicy(role=self.model_role)
        if (
            not self.authority_snapshot
            and self.capability_policy is None
            and not self.capability_allow
        ):
            # A hand-authored legacy template has no creator authority to
            # inherit. Keep it capability-free until a service-owned schedule
            # snapshot is supplied.
            capability_policy = CapabilityPolicy(deny=("*",))
        metadata = dict(self.metadata)
        lineage = dict(metadata.get("_schedule_lineage") or {})
        lineage["continuity"] = self.continuity
        if self.authority_snapshot.get("principal") and "principal_id" not in lineage:
            lineage["principal_id"] = self.authority_snapshot["principal"].get("principal_id")
        metadata["_schedule_lineage"] = lineage
        if occurrence_key is not None:
            metadata["_occurrence"] = occurrence_key
        metadata.setdefault(
            "autonomy", str(self.authority_snapshot.get("autonomy") or self.autonomy)
        )
        if self.authority_snapshot:
            metadata["_authority_snapshot"] = dict(self.authority_snapshot)
        # Fresh/previous-result/job-memory occurrences always mint a new
        # execution session. Only the explicit ``session`` continuity mode may
        # use the schedule-owned stable session. The fallback to session_id is
        # for old persisted templates and is never used for fresh mode.
        session_id = (
            self.continuity_session_id or self.session_id or new_id("session")
            if self.continuity == "session"
            else new_id("session")
        )
        scheduled_delivery = (
            _delivery_from_record(self.authority_snapshot.get("delivery"))
            if "delivery" in self.authority_snapshot
            else self.delivery
            or (None if not self.delivery_channel else DeliverySpec(channel=self.delivery_channel))
        )
        return TaskSpec(
            id=new_id("task"),
            objective=self.objective,
            acceptance_criteria=self.acceptance_criteria,
            session_id=session_id,
            parent_task_id=self.parent_task_id,
            workspace=workspace,
            capability_policy=capability_policy,
            model_policy=model_policy,
            resource_budget=budget,
            deadline=self.deadline,
            delivery=scheduled_delivery,
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
    authority = meta.get("_authority_snapshot") if isinstance(meta, dict) else None
    authority = dict(authority) if isinstance(authority, Mapping) else {}
    deadline = _to_dt(active.get("deadline"))
    return TaskTemplate(
        objective=active.get("objective") or job.get("name") or "",
        session_id=active.get("session_id"),
        continuity_session_id=active.get("continuity_session_id"),
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
        delivery=_delivery_from_record(active.get("delivery")),
        continuity=str(active.get("continuity") or "fresh"),
        acceptance_criteria=_criteria_from_records(active.get("acceptance_criteria")),
        metadata=dict(active.get("metadata") or {}),
        authority_snapshot=authority,
    )


def _path_rules(raw: Any) -> tuple:
    from athena.protocol.tasks import PathRule

    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(
        PathRule(path=str(item.get("path") or ""), allow=bool(item.get("allow", True)))
        for item in raw
        if isinstance(item, Mapping) and item.get("path")
    )


def _delivery_from_record(raw: Any) -> DeliverySpec | None:
    if not isinstance(raw, Mapping) or not raw.get("channel"):
        return None
    return DeliverySpec(
        channel=str(raw["channel"]),
        destination=(str(raw["destination"]) if raw.get("destination") is not None else None),
    )


def _capability_policy_from_record(raw: Any) -> CapabilityPolicy | None:
    if not isinstance(raw, Mapping):
        return None
    return CapabilityPolicy(
        effects=frozenset(str(value) for value in raw.get("effects") or ()),
        allow=tuple(str(value) for value in raw.get("allow") or ()),
        ask=tuple(str(value) for value in raw.get("ask") or ()),
        deny=tuple(str(value) for value in raw.get("deny") or ()),
    )


def _model_policy_from_record(raw: Any) -> ModelPolicy | None:
    if not isinstance(raw, Mapping):
        return None
    from athena.api.decoders import decode_model_policy

    return decode_model_policy(raw)


def _budget_from_record(raw: Any) -> ResourceBudget | None:
    if not isinstance(raw, Mapping):
        return None
    from athena.api.decoders import decode_budget

    return decode_budget(raw)


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
                evidence_required=bool(item.get("evidence_required", False)),
                evidence_requirement_id=(
                    str(item["evidence_requirement_id"])
                    if item.get("evidence_requirement_id") is not None
                    else None
                ),
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
        admission: Any = None,
        intake: Any = None,
        max_concurrent: int = 0,
        loop_interval_seconds: float = 1.0,
    ) -> None:
        self._store = store
        self._tm = task_manager
        self._admission = admission
        self._intake = intake
        self._max_concurrent = max_concurrent
        self._loop_interval = loop_interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Every claim-producing entry point shares one boundary.  The
        # background tick loop, an operator-triggered run, and an event
        # callback can otherwise race on the same SQLite-backed schedule and
        # make a valid task look failed under lock contention.
        self._claim_lock = asyncio.Lock()
        self._deferred_events: deque[Any] = deque()
        self._event_drain_task: asyncio.Task | None = None
        self._health: dict[str, Any] = {
            "started_at": None,
            "last_tick_at": None,
            "last_success_at": None,
            "last_error_at": None,
            "last_error": None,
            "consecutive_failures": 0,
            "reconciliation_failures": 0,
            "health": "stopped",
        }

    async def tick(self, now: datetime | None = None) -> int:
        """Claim and enqueue due jobs for this tick. Returns number fired."""
        async with self._claim_boundary():
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

    async def run_now(self, job_id: str) -> str | None:
        """Run one enabled job occurrence through the normal claim path.

        Operator-triggered runs retain the same durable claim, task metadata,
        admission, and receipt semantics as scheduled runs.  They are not a
        second execution loop and are deliberately refused for disabled or
        missing jobs.
        """
        async with self._claim_boundary():
            job = await self._store.get_job_id(job_id)
            if job is None or not bool(job.get("enabled")):
                return None
            scheduled_for = utcnow().isoformat()
            claim = await self._store.claim_next_due(job_id, scheduled_for)
            if claim is None:
                return None
            try:
                await self._fire_claim(job, _to_claim(claim))
            except Exception:
                # _fire_claim releases an unmaterialized claim; preserve the
                # exception for the operator instead of reporting a false run.
                raise
            run = await self._store.last_run(job_id)
            return str(run.get("task_id")) if run and run.get("task_id") else None

    async def notify_event(self, event: Any) -> int:
        # EventStore delivers durable callbacks synchronously. If a task
        # creation event is emitted while tick/run_now already owns the claim
        # boundary, queue the event and drain it through that same serialized
        # boundary after the owner releases it. External event deliveries take
        # the boundary directly, so all three claim entry points share one
        # concurrency model.
        if _claim_context.get():
            self._deferred_events.append(event)
            self._ensure_event_drain()
            return 0
        try:
            async with self._claim_boundary():
                return await self._notify_event(event)
        except Exception as exc:
            self._record_error(exc)
            raise

    @asynccontextmanager
    async def _claim_boundary(self):
        async with self._claim_lock:
            token = _claim_context.set(True)
            try:
                yield
            finally:
                _claim_context.reset(token)

    def _ensure_event_drain(self) -> None:
        if self._event_drain_task is None or self._event_drain_task.done():
            self._event_drain_task = asyncio.create_task(self._drain_deferred_events())

    async def _drain_deferred_events(self) -> None:
        try:
            while self._deferred_events:
                event = self._deferred_events.popleft()
                async with self._claim_boundary():
                    await self._notify_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_error(exc)
            _logger.warning("deferred scheduler event failed: %s", exc)
        finally:
            if self._deferred_events and not self._stop.is_set():
                self._ensure_event_drain()

    async def _notify_event(self, event: Any) -> int:
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
        self._record_success()
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
        if template.continuity in {"previous_result", "job_memory"}:
            prior = await self._store.previous_completed_run(job["id"], claim.scheduled_for)
            if prior and prior.get("task_id"):
                result = await self._tm.get_result(str(prior["task_id"]))
                if result is not None:
                    metadata["_schedule_previous_result"] = {
                        "task_id": str(result.task_id),
                        "status": result.status.value,
                        "summary": str(result.summary or "")[:12_000],
                        "unresolved": list(result.unresolved)[:64],
                    }
                    if template.continuity == "job_memory":
                        metadata["_schedule_job_memory"] = dict(
                            metadata["_schedule_previous_result"]
                        )
        template = replace(template, metadata=metadata)
        spec = template.build_task_spec(job["id"], occurrence_key=occurrence_key)
        created = None
        try:
            if self._intake is not None:
                result = self._intake(spec, wait=False, trusted=True, enqueue=False)
                created = await result if asyncio.iscoroutine(result) else result
            else:
                if self._admission is not None:
                    result = self._admission(spec)
                    if asyncio.iscoroutine(result):
                        await result
                created = await self._tm.create(spec)
                if created is None:
                    raise RuntimeError(
                        "TaskManager.create returned no Task for scheduled occurrence"
                    )
            if created is None:
                raise RuntimeError("TaskManager.create returned no Task for scheduled occurrence")
        except Exception:
            # A successful create followed by enqueue failure leaves a real
            # CREATED task that reconciliation can enqueue. Releasing that
            # claim would permit a duplicate Task for the same occurrence.
            if created is None:
                await self._store.release_claim(claim.claim_id, job["id"], claim.scheduled_for)
            raise
        task_id = created.id
        # Enqueue only after the durable task row exists, but before marking
        # the occurrence FIRED.  If enqueue fails, the claim remains CLAIMED
        # and startup reconciliation can retry the existing task without
        # creating a duplicate occurrence.
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
        self._health["started_at"] = utcnow().isoformat()
        self._health["health"] = "recovering"
        try:
            await self.reconcile()
        except Exception as exc:
            self._record_error(exc, reconciliation=True)
            self._health["health"] = "failed"
            raise
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
        reconcile = self._store.reconcile_stale_occurrences
        if isinstance(self._store, ScheduleStore):
            await reconcile(
                task_manager=self._tm,
                next_run_resolver=self._recovery_schedule,
            )
        else:
            # Keep small test/embedding stores compatible with the original
            # zero-argument reconciliation protocol.
            await reconcile()

    async def _recovery_schedule(
        self, job: dict[str, Any], scheduled_for: str
    ) -> tuple[str | None, bool]:
        next_run, exhausted = self._next_run(
            job,
            Claim(claim_id="recovery", job_id=str(job["id"]), scheduled_for=scheduled_for),
        )
        trigger = _trigger_from_job(job)
        disable = exhausted
        if trigger is not None and trigger.times is not None:
            disable = disable or await self._store.count_runs(job["id"]) >= trigger.times
        return next_run, disable

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        drain = self._event_drain_task
        if drain is not None and not drain.done():
            try:
                await asyncio.wait_for(asyncio.shield(drain), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                drain.cancel()
            except Exception as exc:
                self._record_error(exc)
        self._event_drain_task = None
        self._health["health"] = "stopped"

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

    def health(self) -> dict[str, Any]:
        """Return scheduler health independently of coroutine liveness."""
        report = dict(self._health)
        report["running"] = self.is_running()
        # Compatibility for embedders that install a live loop task directly;
        # a real start() always moves the state to recovering first.
        if report["running"] and report["health"] == "stopped":
            report["health"] = "healthy"
        return report

    def _record_error(self, exc: Exception, *, reconciliation: bool = False) -> None:
        now = utcnow().isoformat()
        self._health["last_error_at"] = now
        self._health["last_error"] = str(exc)
        self._health["consecutive_failures"] = int(self._health.get("consecutive_failures", 0)) + 1
        if reconciliation:
            self._health["reconciliation_failures"] = (
                int(self._health.get("reconciliation_failures", 0)) + 1
            )
        self._health["health"] = (
            "failed" if self._health["consecutive_failures"] >= 3 else "degraded"
        )

    def _record_success(self) -> None:
        previous = str(self._health.get("health") or "")
        self._health["last_success_at"] = utcnow().isoformat()
        self._health["consecutive_failures"] = 0
        self._health["health"] = "recovering" if previous == "failed" else "healthy"

    async def _run(self) -> None:
        # Give callers one scheduling turn after startup to finish durable
        # setup or perform an explicit tick.  Immediate first-pass polling
        # makes a due occurrence race with recovery/bootstrap code.
        try:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._loop_interval)
            except asyncio.TimeoutError:
                pass
            while not self._stop.is_set():
                self._health["last_tick_at"] = utcnow().isoformat()
                try:
                    await self.tick()
                except Exception as exc:
                    self._record_error(exc)
                    _logger.warning("scheduler tick failed: %s", exc)
                    # If a claim was left open, attempt immediate reconciliation
                    try:
                        await self.reconcile()
                    except Exception as reconcile_error:
                        self._record_error(reconcile_error, reconciliation=True)
                        _logger.warning("scheduler reconciliation failed: %s", reconcile_error)
                else:
                    self._record_success()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._loop_interval)
                except asyncio.TimeoutError:
                    continue
        except Exception as exc:
            self._record_error(exc)
            self._health["health"] = "failed"
            raise


def _filters_match(filters: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    """Match scalar event filters exactly; nested mappings use equality."""
    for key, expected in dict(filters or {}).items():
        if payload.get(key) != expected:
            return False
    return True


__all__ = ["ScheduledJob", "Scheduler", "TaskTemplate", "TriggerSpec"]
