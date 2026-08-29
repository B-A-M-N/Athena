from __future__ import annotations

import asyncio
import json
import inspect
import logging
from datetime import datetime
from dataclasses import dataclass, replace
from typing import Any, Mapping

from aiosqlite import IntegrityError

from athena.protocol.events import Event, make_event
from athena.state.database import Database

_logger = logging.getLogger("athena.events")

FAST_EVENT_TYPES = frozenset(
    {
        "ModelDelta",
        "ModelReasoningDelta",
        "StdoutChunk",
        "StderrChunk",
        "CapabilityProgress",
    }
)


@dataclass
class _Subscription:
    callback: Any
    event_types: frozenset[str] | None = None
    excluded_types: frozenset[str] = frozenset()
    queue: asyncio.Queue | None = None
    worker: asyncio.Task | None = None

    def accepts(self, event_type: str) -> bool:
        return event_type not in self.excluded_types and (
            self.event_types is None or event_type in self.event_types
        )


class EventStore:
    """Append-only, idempotent event log (BUILDSPEC sections 78-83).

    Events are immutable. ``append_event`` is the SINGLE authoritative append
    path (P0-20): it assigns the per-task ``sequence`` atomically from the
    database (``SELECT COALESCE(MAX(sequence), 0) + 1``) inside a transaction, so the
    ``UNIQUE(task_id, sequence)`` constraint can never collide and no event is
    silently dropped — regardless of how many emitters write for the same task
    or across process restarts. No component may manufacture its own sequence.

    Consumers tolerate duplicate delivery, so re-driven events deduplicate by
    stable event id on append (section 81).
    """

    _COLS = "id, task_id, session_id, type, sequence, timestamp, schema_version, payload, causal_id"

    def __init__(self, db: Database) -> None:
        self._db = db
        self._subscribers: list[_Subscription] = []
        self._append_condition = asyncio.Condition()
        self._append_generation = 0

    def subscribe(
        self,
        callback: Any,
        *,
        event_types: set[str] | frozenset[str] | tuple[str, ...] | None = None,
        exclude_event_types: set[str] | frozenset[str] | tuple[str, ...] = (),
    ) -> None:
        """Register an event observer after durable append succeeds.

        Stream events are delivered through a bounded, coalescing queue so a
        slow projection cannot backpressure provider output. Control/evidence
        events retain the awaited subscriber semantics used by durable
        projections. ``event_types`` and ``exclude_event_types`` keep broad
        observers from receiving noise they cannot act on.
        """
        selected = None if event_types is None else frozenset(map(str, event_types))
        excluded = frozenset(map(str, exclude_event_types))
        for subscription in self._subscribers:
            if subscription.callback == callback:
                subscription.event_types = selected
                subscription.excluded_types = excluded
                return
        self._subscribers.append(
            _Subscription(
                callback=callback,
                event_types=selected,
                excluded_types=excluded,
            )
        )

    def unsubscribe(self, callback: Any) -> None:
        """Remove an event observer without affecting the event log."""
        remaining: list[_Subscription] = []
        for subscription in self._subscribers:
            if subscription.callback == callback:
                if subscription.worker is not None:
                    subscription.worker.cancel()
                continue
            remaining.append(subscription)
        self._subscribers = remaining

    @property
    def append_generation(self) -> int:
        """Return a process-local generation for same-process live streams."""
        return self._append_generation

    async def wait_for_append(self, after_generation: int) -> int:
        """Wake when a same-process append occurs.

        Callers still use a bounded timeout around this method because another
        Athena process may append to the same database without sharing this
        in-process condition.
        """
        async with self._append_condition:
            while self._append_generation <= after_generation:
                await self._append_condition.wait()
            return self._append_generation

    async def append_event(
        self,
        type_: str,
        payload: Mapping[str, Any] | None = None,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        causal_id: str | None = None,
        id: str | None = None,
    ) -> Event:
        while True:
            try:
                async with self._db.transaction() as db:
                    row = await db.fetch_one_raw(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 AS seq "
                        "FROM events WHERE task_id = ?",
                        (task_id,),
                    )
                    seq = int(row["seq"]) if row and row.get("seq") else 1
                    event = make_event(
                        type=type_,
                        payload=payload,
                        task_id=task_id,
                        session_id=session_id,
                        sequence=seq,
                        id=id,
                        causal_id=causal_id,
                    )
                    await db.execute_raw(
                        f"INSERT INTO events({self._COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        self._values(event),
                    )
                await self._notify_append()
                for callback in tuple(self._subscribers):
                    if callback.accepts(event.type):
                        if event.type in FAST_EVENT_TYPES:
                            self._enqueue_fast(callback, event)
                        else:
                            await self._deliver(callback, event)
                return event
            except IntegrityError as exc:
                # Retry only on a genuine (task_id, sequence) UNIQUE collision
                # from a concurrent writer. FK violations must propagate.
                if "UNIQUE" not in str(exc):
                    raise
                continue

    async def append(self, event: Event) -> None:
        if event.id and await self._db.fetch_one("SELECT id FROM events WHERE id = ?", (event.id,)):
            return
        await self.append_event(
            event.type,
            event.payload,
            task_id=event.task_id,
            session_id=event.session_id,
            causal_id=event.causal_id,
            id=event.id,
        )

    async def _notify_append(self) -> None:
        async with self._append_condition:
            self._append_generation += 1
            self._append_condition.notify_all()

    async def _deliver(self, subscription: _Subscription, event: Event) -> None:
        try:
            outcome = subscription.callback(event)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:
            # Observers are projections. A broken observer must never turn a
            # committed canonical event into a failed write or cause a
            # producer to retry it.
            _logger.warning(
                "event subscriber failed for %s",
                event.type,
                exc_info=True,
            )

    def _enqueue_fast(self, subscription: _Subscription, event: Event) -> None:
        if subscription.queue is None:
            subscription.queue = asyncio.Queue(maxsize=64)
        queue = subscription.queue
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pending: list[Event] = []
            while True:
                try:
                    pending.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            pending.append(event)
            pending = _coalesce_fast_events(pending)
            for item in pending[-queue.maxsize :]:
                queue.put_nowait(item)
        if subscription.worker is None or subscription.worker.done():
            subscription.worker = asyncio.create_task(self._drain_fast(subscription))

    async def _drain_fast(self, subscription: _Subscription) -> None:
        queue = subscription.queue
        if queue is None:
            return
        while True:
            event = await queue.get()
            try:
                await self._deliver(subscription, event)
            finally:
                queue.task_done()

    @staticmethod
    def _values(event: Event) -> tuple:
        return (
            event.id,
            event.task_id,
            event.session_id,
            event.type,
            event.sequence,
            event.timestamp.isoformat(),
            event.schema_version,
            json.dumps(dict(event.payload)),
            event.causal_id,
        )

    async def list_for_task(
        self,
        task_id: str,
        after_sequence: int = 0,
    ) -> list[Event]:
        rows = await self._db.fetch_all(
            "SELECT * FROM events WHERE task_id = ? AND sequence > ? ORDER BY sequence ASC",
            (task_id, after_sequence),
        )
        return [_row_to_event(r) for r in rows]

    async def list_recent(
        self,
        *,
        after_rowid: int = 0,
        limit: int = 200,
    ) -> list[Event]:
        """Global tail across all tasks, ordered by insertion (rowid).

        ``after_rowid`` is the last rowid seen by the caller (the viewer's
        cursor); only newer events are returned. Distinct from per-task
        ``sequence``, which resets per task.
        """
        rows = await self._db.fetch_all(
            "SELECT *, rowid AS _rid FROM events WHERE rowid > ? ORDER BY rowid ASC LIMIT ?",
            (after_rowid, int(limit)),
        )
        out = []
        for r in rows:
            ev = _row_to_event(r)
            try:
                rid = int(r.get("_rid") or 0)
            except (TypeError, ValueError):
                rid = after_rowid
            object.__setattr__(ev, "_rowid", rid)  # frozen dataclass
            out.append(ev)
        return out

    async def list_for_session(self, session_id: str) -> list[Event]:
        rows = await self._db.fetch_all(
            "SELECT * FROM events WHERE session_id = ? ORDER BY timestamp ASC, sequence ASC",
            (session_id,),
        )
        return [_row_to_event(r) for r in rows]

    async def last_sequence(self, task_id: str) -> int:
        row = await self._db.fetch_one(
            "SELECT MAX(sequence) AS seq FROM events WHERE task_id = ?",
            (task_id,),
        )
        return int((row or {}).get("seq") or 0)


def _row_to_event(row: dict | Any) -> Event:
    payload = json.loads(row["payload"]) if row.get("payload") else {}
    return Event(
        id=row["id"],
        type=row["type"],
        sequence=row["sequence"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        task_id=row.get("task_id"),
        session_id=row.get("session_id"),
        schema_version=row.get("schema_version", 1),
        payload=payload,
        causal_id=row.get("causal_id"),
    )


def _coalesce_fast_events(events: list[Event]) -> list[Event]:
    """Coalesce adjacent presentation chunks while retaining control order."""
    output: list[Event] = []
    keys = {
        "ModelDelta": "text",
        "StdoutChunk": "data",
        "StderrChunk": "data",
    }
    for event in events:
        if output:
            previous = output[-1]
            key = keys.get(event.type)
            if (
                key is not None
                and previous.type == event.type
                and previous.task_id == event.task_id
                and previous.session_id == event.session_id
            ):
                payload = dict(previous.payload)
                payload[key] = str(payload.get(key) or "") + str(event.payload.get(key) or "")
                output[-1] = replace(event, payload=payload)
                continue
        output.append(event)
    return output


__all__ = ["EventStore", "FAST_EVENT_TYPES"]
