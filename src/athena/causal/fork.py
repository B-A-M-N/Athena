"""Causal task fork: re-run a task from a chosen point in its event history.

A fork creates a NEW task that copies the original's objective, acceptance
criteria, workspace, and capability policy, records where it forked from in
its metadata, and enters the queue as independent work. The parent task and
its event log are never mutated.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from athena.protocol.ids import new_id

_logger = logging.getLogger(__name__)


class TaskForker:
    """Create causal forks of existing tasks and inspect their timelines."""

    def __init__(self, service: Any = None) -> None:
        self._service = service

    async def fork(
        self,
        *,
        task_id: str,
        after_event_sequence: int,
        model_policy_override: Any = None,
    ) -> dict:
        """Fork ``task_id``, resuming conceptually after event sequence N."""
        if self._service is None:
            raise RuntimeError("TaskForker requires an AthenaService instance")
        store_tasks = self._service._store_tasks
        tm = self._service._task_manager
        if store_tasks is None or tm is None:
            raise RuntimeError("AthenaService not started")

        row = await store_tasks.get(task_id)
        if row is None:
            raise KeyError(f"Task not found: {task_id}")

        from athena.kernel.lifecycle import deserialize_task

        spec = deserialize_task(dict(row))
        metadata = dict(spec.metadata or {})
        # Drop runtime bookkeeping inherited from the parent.
        metadata.pop("status", None)
        metadata["fork_of"] = task_id
        metadata["fork_after_event"] = int(after_event_sequence)

        new_spec = dataclasses.replace(
            spec,
            id=new_id("task"),
            metadata=metadata,
            model_policy=model_policy_override or spec.model_policy,
        )
        created = await tm.create(new_spec)
        await tm.enqueue(created.id)
        _logger.info(
            "forked task %s -> %s (after_event=%s)",
            task_id,
            created.id,
            after_event_sequence,
        )
        return {
            "fork_id": created.id,
            "parent": task_id,
            "resumed_at_event": int(after_event_sequence),
        }

    async def timeline(self, task_id: str) -> list[dict]:
        """Summarize a task's events so an operator can pick a fork point."""
        if self._service is None:
            raise RuntimeError("TaskForker requires an AthenaService instance")
        events = self._service._store_events
        if events is None:
            raise RuntimeError("AthenaService not started")
        out: list[dict] = []
        for ev in await events.list_for_task(task_id):
            payload = dict(ev.payload or {})
            summary_keys = ("summary", "status", "objective", "reason", "result",
                            "message", "artifact", "path", "command", "error")
            bits = {k: payload[k] for k in summary_keys if k in payload}
            if not bits:
                bits = {k: payload[k] for k in list(payload)[:3]}
            out.append({
                "sequence": ev.sequence,
                "type": ev.type,
                "timestamp": ev.timestamp.isoformat(),
                "event_id": ev.id,
                "payload_bits": bits,
            })
        return out


__all__ = ["TaskForker"]
