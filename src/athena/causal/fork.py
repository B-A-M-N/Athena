"""Causal task fork: re-run a task from a chosen point in its event history.

    A fork creates a NEW task that copies the original's objective, acceptance
criteria, and capability policy, records where it forked from in its metadata,
and enters the queue as independent work. When a checkpoint is supplied, its
workspace is materialized into an independent temporary root; otherwise the
current workspace is retained explicitly. The parent task and event log are
never mutated.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import shutil
import tempfile
from typing import Any

from athena.protocol.ids import new_id
from athena.protocol.tasks import WorkspaceSpec

_logger = logging.getLogger(__name__)


class TaskForker:
    """Create causal forks of existing tasks and inspect their timelines."""

    def __init__(self, service: Any = None, checkpoint_manager: Any = None) -> None:
        self._service = service
        self._checkpoint_manager = checkpoint_manager

    async def fork(
        self,
        *,
        task_id: str,
        after_event_sequence: int,
        model_policy_override: Any = None,
        workspace_checkpoint_id: str | None = None,
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

        if after_event_sequence < 0:
            raise ValueError("after_event_sequence must be non-negative")
        events = self._service._store_events
        if events is None:
            raise RuntimeError("AthenaService event store is not started")
        timeline = await events.list_for_task(task_id)
        last_sequence = timeline[-1].sequence if timeline else 0
        if after_event_sequence > last_sequence:
            raise ValueError(
                f"cannot fork after event {after_event_sequence}; "
                f"task ends at event {last_sequence}"
            )
        prefix = [event for event in timeline if event.sequence <= after_event_sequence]
        boundary = prefix[-1] if prefix else None

        from athena.kernel.lifecycle import deserialize_task

        spec = deserialize_task(dict(row))
        metadata = dict(spec.metadata or {})
        # Drop runtime bookkeeping inherited from the parent.
        metadata.pop("status", None)
        metadata["fork_of"] = task_id
        metadata["fork_after_event"] = int(after_event_sequence)
        metadata["causal_reconstruction"] = {
            "event_count": len(prefix),
            "event_ids": [event.id for event in prefix],
            "event_prefix_sha256": _event_prefix_digest(prefix),
            "boundary_timestamp": boundary.timestamp.isoformat() if boundary else None,
            # A file snapshot is intentionally not implied by an event prefix.
            # Fusion supplies a checkpoint when it needs workspace rollback;
            # ordinary forks retain the current workspace explicitly.
            "workspace_state": (
                "checkpoint" if workspace_checkpoint_id else "current_at_fork"
            ),
            "workspace_checkpoint_id": workspace_checkpoint_id,
        }

        fork_id = new_id("task")
        workspace = spec.workspace
        fork_root: str | None = None
        if workspace_checkpoint_id is not None:
            if self._checkpoint_manager is None:
                raise RuntimeError(
                    "workspace_checkpoint_id requires a CheckpointManager")
            if workspace is None:
                raise ValueError(
                    "workspace_checkpoint_id requires a task workspace")
            fork_root = tempfile.mkdtemp(prefix=f"athena-fork-{fork_id}-")
            try:
                await self._checkpoint_manager.materialize(
                    workspace_checkpoint_id, fork_root)
            except Exception:
                # A failed materialization must not leave a task pointing at
                # an empty partial fork workspace.
                shutil.rmtree(fork_root, ignore_errors=True)
                raise
            workspace = WorkspaceSpec(
                id=f"{workspace.id}/fork/{fork_id}",
                root=fork_root,
                readable=workspace.readable,
                writable=workspace.writable,
                temp_root=workspace.temp_root,
                execution_backend=workspace.execution_backend,
                network_policy=workspace.network_policy,
            )
            metadata["fork_workspace_root"] = fork_root

        # Materialize the independent workspace before cloning the session.
        # If checkpoint restoration fails, this ordering prevents an orphaned
        # fork session from surviving without a corresponding child task.
        try:
            session_id = await self._clone_session_prefix(
                spec.session_id,
                boundary_timestamp=boundary.timestamp if boundary else None,
                parent_task_id=task_id,
                after_event_sequence=after_event_sequence,
            )
        except Exception:
            if fork_root is not None:
                shutil.rmtree(fork_root, ignore_errors=True)
            raise
        if session_id is not None:
            metadata["fork_session_id"] = session_id

        new_spec = dataclasses.replace(
            spec,
            id=fork_id,
            session_id=session_id or spec.session_id,
            workspace=workspace,
            metadata=metadata,
            model_policy=model_policy_override or spec.model_policy,
        )
        try:
            created = await tm.create(new_spec)
        except Exception:
            # TaskManager normally inserts the task before emitting lifecycle
            # events.  Check before removing speculative state: if a late
            # event/budget hook failed after insertion, the durable task owns
            # the fork session and workspace and both must remain recoverable.
            task_exists = True
            try:
                task_exists = await store_tasks.get(fork_id) is not None
            except Exception:
                _logger.exception(
                    "could not determine ownership of failed fork %s", fork_id
                )
            if not task_exists:
                sessions = getattr(self._service, "_sessions", None)
                if session_id is not None and sessions is not None:
                    try:
                        await sessions.delete_if_orphaned(session_id)
                    except Exception:
                        _logger.exception(
                            "failed to remove orphaned fork session %s", session_id
                        )
                if fork_root is not None:
                    shutil.rmtree(fork_root, ignore_errors=True)
            raise
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

    async def _clone_session_prefix(
        self,
        session_id: str | None,
        *,
        boundary_timestamp,
        parent_task_id: str,
        after_event_sequence: int,
    ) -> str | None:
        """Create a session containing only messages at the causal boundary.

        Messages are append-only and are not keyed to event sequences. The
        durable event timestamp is therefore the conservative join boundary:
        a message created after the chosen event is never copied. The copied
        message IDs and session metadata make the reconstruction auditable.
        """
        sessions = getattr(self._service, "_sessions", None)
        messages = getattr(self._service, "_store_messages", None)
        if session_id is None or sessions is None or messages is None:
            return None

        count = await messages.count_session_messages(session_id)
        source = await messages.list_session_messages(
            session_id, limit=max(count, 1), offset=0,
        )
        selected = [
            message for message in source
            if boundary_timestamp is None or message.created_at <= boundary_timestamp
        ]
        fork_session_id = new_id("session")
        try:
            await sessions.create(
                fork_session_id,
                parent_id=session_id,
                metadata={
                    "causal_fork_of_task": parent_task_id,
                    "causal_fork_after_event": after_event_sequence,
                    "causal_source_session": session_id,
                    "causal_message_count": len(selected),
                },
            )
            from dataclasses import replace

            for message in selected:
                cloned = replace(
                    message,
                    id=new_id("msg"),
                    metadata={
                        **dict(message.metadata or {}),
                        "causal_fork_source_message": message.id,
                        "causal_fork_source_session": session_id,
                    },
                )
                await messages.append_to_session(fork_session_id, cloned)
        except Exception:
            # Session creation and message cloning are a single speculative
            # setup phase.  Remove a partial transcript if any append fails;
            # no task can reference this session yet.
            try:
                await sessions.delete_if_orphaned(fork_session_id)
            except Exception:
                _logger.exception(
                    "failed to remove partial fork session %s", fork_session_id
                )
            raise
        return fork_session_id

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


def _event_prefix_digest(events: list[Any]) -> str:
    payload = [
        {
            "id": event.id,
            "type": event.type,
            "sequence": event.sequence,
            "timestamp": event.timestamp.isoformat(),
            "payload": dict(event.payload or {}),
        }
        for event in events
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
