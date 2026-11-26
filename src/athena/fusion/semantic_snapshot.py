"""Bounded semantic-state projection for Fusion checkpoints.

The checkpoint manager owns file snapshots and retention.  This module only
projects the durable task/event/world/runtime/context/affordance facts that
can be attached to a checkpoint as descriptive evidence; it never mutates or
restores application state.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from athena.protocol.messages import utcnow

_logger = logging.getLogger("athena.fusion.semantic_snapshot")


class SemanticSnapshot:
    """Assemble a bounded JSON-safe checkpoint evidence envelope."""

    def __init__(
        self,
        *,
        shadow: Any,
        task_store: Any | None = None,
        event_store: Any | None = None,
        runtime_session_store: Any | None = None,
        context_block_store: Any | None = None,
        fabric: Any | None = None,
        workflow_store: Any | None = None,
        world_state_store: Any | None = None,
        world_state_provider: Callable[[str], Any] | None = None,
    ) -> None:
        self._shadow = shadow
        self._task_store = task_store
        self._event_store = event_store
        self._runtime_session_store = runtime_session_store
        self._context_block_store = context_block_store
        self._fabric = fabric
        self._workflow_store = workflow_store
        self._world_state_store = world_state_store
        self._world_state_provider = world_state_provider

    async def capture(self, *, task_id: str, workspace_root: str) -> dict[str, Any]:
        task = await self._task_snapshot(task_id)
        events = await self._event_snapshot(task_id)
        world_state = await self._world_snapshot(task_id, workspace_root)
        runtimes = await self._runtime_snapshot(task_id)
        contexts = await self._context_snapshot(task_id)
        affordances = await self._affordance_snapshot(task_id)
        branches = self._branch_snapshot(task_id)
        return {
            "captured_at": utcnow().isoformat(),
            "task": task,
            "event_boundary": events,
            "world_state": world_state,
            "attached_context": contexts,
            "runtime_sessions": runtimes,
            "affordances": affordances,
            "shadow_branches": branches,
        }

    async def _task_snapshot(self, task_id: str) -> dict[str, Any] | None:
        if self._task_store is None:
            return None
        try:
            row = await self._task_store.get(task_id)
            if row is None:
                return None
            return {
                key: row.get(key)
                for key in (
                    "id",
                    "status",
                    "objective",
                    "session_id",
                    "parent_task_id",
                    "acceptance_criteria",
                    "context_refs",
                    "workspace",
                    "capability_policy",
                    "model_policy",
                    "resource_budget",
                    "deadline",
                    "delivery",
                )
                if key in row
            }
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            _logger.warning("semantic checkpoint task snapshot failed: %s", exc)
            return None

    async def _event_snapshot(self, task_id: str) -> dict[str, Any]:
        if self._event_store is None:
            return {"last_sequence": 0, "recent_types": []}
        try:
            timeline = await self._event_store.list_for_task(task_id)
            return {
                "last_sequence": max((int(event.sequence or 0) for event in timeline), default=0),
                "recent_types": [str(event.type) for event in timeline[-20:]],
            }
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            _logger.warning("semantic checkpoint event snapshot failed: %s", exc)
            return {"last_sequence": 0, "recent_types": []}

    async def _world_snapshot(self, task_id: str, workspace_root: str) -> dict[str, Any]:
        if self._world_state_provider is None:
            return {}
        try:
            world = self._world_state_provider(task_id)
            snapshot = getattr(world, "snapshot", None)
            if snapshot is None:
                return {}
            value = snapshot(workspace_root=workspace_root)
            if hasattr(value, "__await__"):
                value = await value
            return dict(value or {})
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            _logger.warning("semantic checkpoint world snapshot failed: %s", exc)
            return {}

    async def _runtime_snapshot(self, task_id: str) -> list[dict[str, Any]]:
        if self._runtime_session_store is None:
            return []
        try:
            rows = await self._runtime_session_store.list_for_task(task_id)
            keys = (
                "id",
                "backend",
                "runtime",
                "cwd",
                "pid",
                "is_alive",
                "started_at",
                "last_heartbeat",
                "ended_at",
            )
            return [{key: row[key] for key in keys if key in row} for row in rows]
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            _logger.warning("semantic checkpoint runtime snapshot failed: %s", exc)
            return []

    async def _context_snapshot(self, task_id: str) -> list[dict[str, Any]]:
        if self._context_block_store is None:
            return []
        try:
            blocks = await self._context_block_store.list(
                scopes=(("task", task_id),), attached_only=False
            )
            return [
                {
                    "id": block.id,
                    "version": block.version,
                    "label": block.label,
                    "scope": block.scope,
                    "scope_id": block.scope_id,
                    "attached": block.attached,
                }
                for block in blocks
            ]
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            _logger.warning("semantic checkpoint context snapshot failed: %s", exc)
            return []

    async def _affordance_snapshot(self, task_id: str) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {"capabilities": [], "workflows": []}
        if self._fabric is not None:
            try:
                result["capabilities"] = [
                    {
                        key: record.get(key)
                        for key in ("id", "scope", "lifecycle_state", "code_hash", "schema_hash")
                    }
                    for record in self._fabric.created_this_task(task_id)
                ]
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                _logger.warning("semantic checkpoint affordance snapshot failed: %s", exc)
        if self._workflow_store is not None:
            try:
                result["workflows"] = [
                    {
                        "id": workflow.id,
                        "version": workflow.version,
                        "scope": workflow.scope.value,
                        "lifecycle_state": workflow.lifecycle_state,
                        "step_count": len(workflow.steps),
                    }
                    for workflow in await self._workflow_store.list(task_id=task_id)
                ]
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                _logger.warning("semantic checkpoint workflow snapshot failed: %s", exc)
        return result

    def _branch_snapshot(self, task_id: str) -> list[dict[str, Any]]:
        try:
            return [
                {
                    "id": branch.id,
                    "status": branch.status,
                    "commit_state": branch.commit_state,
                    "checkpoint_id": branch.checkpoint_id,
                }
                for branch in self._shadow.list_branches()
                if branch.task_id == task_id
            ]
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            _logger.warning("semantic checkpoint branch snapshot failed: %s", exc)
            return []


__all__ = ["SemanticSnapshot"]
