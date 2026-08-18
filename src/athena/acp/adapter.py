"""ACP adapter (§93 ACP Integration / RESEARCHSPEC "ACP / agent client").

ACP is a client interface around AthenaService. Athena MAY expose itself as an
ACP-servable agent (server side) AND act as an ACP client to other agents; for
v1 we implement the **server** side minimally: accept an inbound ACP request (a
task/objective), convert it to a :class:`TaskSpec`, submit it via
:class:`~athena.tasks.manager.TaskManager`, and stream events/results back in
ACP message envelopes.

There MUST NOT be an ``ACPAgent`` with independent reasoning behavior
(BUILDSPEC §93 / INV-001). This adapter creates a Task and the kernel runs it.
ACP MUST NOT maintain an independent session store (INV-003); it routes session
state exclusively through Athena's :class:`~athena.state.sessions.SessionRepository`.

The transport is intentionally thin: ACP envelope <-> Athena Message/Event.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Mapping

from athena.api.decoders import (
    decode_budget,
    decode_capability_policy,
    decode_model_policy,
    decode_workspace,
)
from athena.protocol.ids import new_id
from athena.protocol.messages import (
    Message,
    Provenance,
    SourceType,
    TrustClass,
    utcnow,
)
from athena.protocol.tasks import (
    AutonomyLevel,
    DeliverySpec,
    NetworkPolicy,
    TaskSpec,
)

# ---------------------------------------------------------------------------
# Lightweight ACP envelope model (server side). The ACP wire protocol is thin
# here so we model the minimum we must accept and emit.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ACPRequest:
    """An inbound ACP task/objective request (maps to a TaskSpec)."""

    objective: str
    session_id: str | None = None
    task_id: str | None = None
    parent_session_id: str | None = None
    user: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    capability_policy: Mapping[str, Any] | None = None
    model_policy: Mapping[str, Any] | None = None
    resource_budget: Mapping[str, Any] | None = None
    workspace: Mapping[str, Any] | None = None
    deadline: str | None = None
    delivery: Mapping[str, Any] | None = None
    autonomy: str | None = None


@dataclass(frozen=True)
class ACPEvent:
    """An outbound ACP event envelope."""

    type: str
    task_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    seq: int = 0


# ACP event type tokens.
EV_TASK_ACCEPTED = "task.accepted"
EV_TASK_STARTED = "task.started"
EV_TASK_MESSAGE = "task.message"
EV_TASK_FINISHED = "task.finished"
EV_TASK_ERROR = "task.error"

_STATUS_TO_ACP_ENV: dict[str, str] = {
    "COMPLETE": EV_TASK_FINISHED,
    "PARTIAL": EV_TASK_FINISHED,
    "FAILED": EV_TASK_ERROR,
    "CANCELLED": EV_TASK_ERROR,
    "INTERRUPTED": EV_TASK_ERROR,
    "BLOCKED": EV_TASK_ERROR,
    "RECOVERY_REQUIRED": EV_TASK_ERROR,
    "QUEUED": EV_TASK_STARTED,
    "RUNNING": EV_TASK_STARTED,
}

# Athena event type strings that mark the end of a task lifecycle.
_TERMINAL_EVENT_TYPES = frozenset(
    {
        "TaskCompleted",
        "TaskPartial",
        "TaskBlocked",
        "TaskFailed",
        "TaskCancelled",
        "TaskInterrupted",
    }
)


class ACPAdapter:
    """Thin ACP server-side transport around Athena task/session services.

    It depends on a :class:`~athena.tasks.manager.TaskManager` (for Task intake)
    and a SessionRepository (INV-003). It never owns a session store or a
    reasoning loop.
    """

    def __init__(
        self,
        task_manager: Any,
        sessions: Any,
        *,
        event_store: Any = None,
        stream_poll_interval: float = 0.25,
        stream_timeout: float = 60.0,
    ) -> None:
        self.task_manager = task_manager
        self.sessions = sessions
        self.event_store = event_store
        self._stream_interval = stream_poll_interval
        self._stream_timeout = stream_timeout
        self._seq = 0

    # ------------------------------------------------------------------ #
    # Inbound: ACP request -> TaskSpec -> TaskManager
    # ------------------------------------------------------------------ #
    def to_task_spec(self, request: ACPRequest) -> TaskSpec:
        """Translate an ACP request into a TaskSpec (no loop, no execution).

        Inbound policy fields are mapped into the TaskSpec so a request can
        never silently escalate to a permissive default: when an ACP client
        leaves policy unspecified, the request is defaulted to SAFE (supervised
        autonomy, an empty capability allow-list, and a scoped workspace).
        """
        task_id = request.task_id or new_id("task")
        session_id = request.session_id
        workspace = decode_workspace(
            request.workspace,
            id_fallback=new_id("ws"),
            network_default=NetworkPolicy.DENY,
        )
        capability = decode_capability_policy(request.capability_policy)
        autonomy = _map_autonomy(request.autonomy)
        model_policy = decode_model_policy(request.model_policy)
        return TaskSpec(
            id=task_id,
            objective=request.objective,
            session_id=session_id,
            acceptance_criteria=(),
            context_refs=(),
            workspace=workspace,
            capability_policy=capability,
            model_policy=model_policy,
            resource_budget=decode_budget(request.resource_budget),
            deadline=_parse_deadline(request.deadline),
            delivery=_map_delivery(request.delivery),
            metadata={
                "origin": "acp",
                "autonomy": autonomy,
                **dict(request.metadata),
            },
        )

    async def submit(self, request: ACPRequest) -> ACPEvent:
        """Convert, create and enqueue the Task, then emit an accepted event.

        Session authority stays with Athena: we hand the session id through to
        the TaskSpec and never open our own store (INV-003). If an inbound
        request names no session, one is allocated by the caller-provided
        session service before submission.

        The task id is decided once here (``request.task_id`` or a fresh id) so
        the Task belongs to a single stable identity.
        """
        task_id = request.task_id or new_id("task")
        session_id = request.session_id or await self._ensure_session(request)
        spec = self.to_task_spec(
            replace(
                request,
                session_id=session_id,
                task_id=task_id,
            )
        )
        await self.task_manager.create(spec)
        await self.task_manager.enqueue(spec.id)
        return self._event(EV_TASK_ACCEPTED, spec.id, {"task_id": spec.id, "session_id": session_id})

    # ------------------------------------------------------------------ #
    # Outbound: Athena Task/Event -> ACP envelope
    # ------------------------------------------------------------------ #
    def task_event_to_acp(self, task: Any, status: Any) -> ACPEvent:
        """Map an Athena task/event to an ACP event envelope."""
        status_str = getattr(status, "value", getattr(status, "name", None)) if status is not None else None
        type_ = _STATUS_TO_ACP_ENV.get(status_str or "", EV_TASK_STARTED)
        task_id = _task_id_of(task)
        payload: dict[str, Any] = {"task_id": task_id}
        if type_ is EV_TASK_FINISHED:
            payload["status"] = "completed"
        seq = self._next_seq()
        return ACPEvent(
            type=type_,
            task_id=task_id,
            payload=payload,
            seq=seq,
        )

    def message_to_acp(self, message: Message) -> ACPEvent:
        """Translate an Athena session Message into an ACP message event."""
        return ACPEvent(
            type=EV_TASK_MESSAGE,
            task_id="",
            payload={"text": message.text(), "role": message.role.value},
            session_id=message.metadata.get("session_id"),
            seq=self._next_seq(),
        )

    async def stream(self, task_id: str) -> AsyncIterator[ACPEvent]:
        """Stream the ACP view of a task; yields events AS THEY ARRIVE.

        Polls the event log and/or the session message store at a fixed
        interval, yielding a new ACP envelope for each newly observed message or
        task event (this is a live stream, not a snapshot). It emits a leading
        STARTED event and stops once a terminal task event is observed or the
        stream timeout elapses.
        """
        yield self._event(EV_TASK_STARTED, task_id, {"task_id": task_id})
        session_id = await self._session_id_for_task(task_id)
        seen_message_ids: set[str] = set()
        last_seq = 0
        if self.event_store is not None:
            last_seq = await _safe_await(self.event_store.last_sequence, task_id) or 0
        deadline = time.monotonic() + self._stream_timeout
        done = False
        while not done:
            if time.monotonic() > deadline:
                break
            if self.event_store is not None:
                try:
                    events = await self.event_store.list_for_task(task_id, last_seq)
                except Exception:
                    events = []
                for ev in events:
                    if ev.sequence > last_seq:
                        last_seq = ev.sequence
                    yield _event_from_athena_event(ev, task_id)
                    if _event_type_of(ev) in _TERMINAL_EVENT_TYPES:
                        done = True
            if session_id and self.sessions is not None and hasattr(self.sessions, "list_messages"):
                try:
                    messages = await self.sessions.list_messages(session_id, limit=200)
                except Exception:
                    messages = []
                for m in messages:
                    mid = str(getattr(m, "id", ""))
                    if mid and mid not in seen_message_ids:
                        seen_message_ids.add(mid)
                        yield self.message_to_acp(m)
            if not done:
                await _sleep(self._stream_interval)

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #
    async def _ensure_session(self, request: ACPRequest) -> str:
        if self.sessions is None:
            raise RuntimeError("no session service configured for ACP")
        session_id = new_id("session")
        await self.sessions.create(session_id, parent_id=request.parent_session_id, metadata={"origin": "acp"})
        return session_id

    async def _session_id_for_task(self, task_id: str) -> str | None:
        """Resolve the session that owns a task (P1-43).

        SessionRepository.list_messages keys on the session id, not the task id.
        We look the task up and use its session so messages are fetched for the
        owning session rather than a non-existent task-keyed store.
        """
        get = getattr(self.task_manager, "get", None)
        if get is None:
            return None
        try:
            task = await get(task_id)
        except Exception:
            return None
        return getattr(task, "session_id", None) or getattr(task, "parent_session_id", None)

    def _event(self, type_: str, task_id: str, payload: Mapping[str, Any]) -> ACPEvent:
        return ACPEvent(
            type=type_,
            task_id=task_id,
            payload=dict(payload),
            session_id=(dict(payload) or {}).get("session_id"),
            seq=self._next_seq(),
        )

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq


def _task_id_of(task: Any) -> str:
    """Extract an id from a Task/object/dict for ACP envelopes."""
    if isinstance(task, dict):
        return str(task.get("id") or task.get("task_id") or "")
    return str(getattr(task, "id", "") or getattr(task, "task_id", "") or "")


def _map_autonomy(raw: str | None) -> str:
    value = (raw or AutonomyLevel.SUPERVISED.value).lower()
    try:
        return AutonomyLevel(value).value
    except ValueError:
        return AutonomyLevel.SUPERVISED.value


def _map_delivery(raw: Mapping[str, Any] | None) -> DeliverySpec | None:
    if raw is None or not raw.get("channel"):
        return None
    return DeliverySpec(
        channel=str(raw.get("channel")),
        destination=raw.get("destination"),
    )


def _parse_deadline(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def _event_type_of(ev: Any) -> str:
    return str(getattr(ev, "type", "") or "")


def _event_from_athena_event(ev: Any, task_id: str) -> ACPEvent:
    """Map an Athena Event onto an ACP lifecycle envelope."""
    ev_type = str(getattr(ev, "type", "") or "")
    if ev_type in _TERMINAL_EVENT_TYPES:
        type_ = (
            EV_TASK_FINISHED
            if ev_type in ("TaskCompleted", "TaskPartial")
            else EV_TASK_ERROR
        )
    elif ev_type in ("TaskStarted", "TaskQueued"):
        type_ = EV_TASK_STARTED
    else:
        type_ = EV_TASK_MESSAGE
    payload = dict(getattr(ev, "payload", None) or {})
    payload["task_id"] = task_id
    if type_ is EV_TASK_FINISHED:
        payload["status"] = "completed"
    return ACPEvent(
        type=type_,
        task_id=task_id,
        payload=payload,
        session_id=getattr(ev, "session_id", None),
    )


async def _safe_await(coro: Any, *args: Any) -> Any:
    try:
        value = coro(*args)
        if hasattr(value, "__await__"):
            return await value
        return value
    except Exception:
        return None


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def build_acp_provenance() -> Provenance:
    """Provenance for messages injected on behalf of an ACP client."""
    return Provenance(
        source_type=SourceType.RUNTIME,
        trust=TrustClass.AGENT_CURATED,
        scope="acp",
        created_at=utcnow(),
    )


__all__ = [
    "ACPAdapter",
    "ACPRequest",
    "ACPEvent",
    "build_acp_provenance",
    "EV_TASK_ACCEPTED",
    "EV_TASK_STARTED",
    "EV_TASK_MESSAGE",
    "EV_TASK_FINISHED",
    "EV_TASK_ERROR",
]