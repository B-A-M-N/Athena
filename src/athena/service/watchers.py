"""Watcher polling and claim invalidation boundary."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from typing import Any

from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResult,
    CapabilityResultStatus,
)
from athena.protocol.ids import new_id
from athena.service.watch_ports import WatchPorts

_logger = logging.getLogger("athena.service.watchers")


class ServiceWatchAPI:
    """Run generated observers and invalidate stale world-state claims."""

    def __init__(self, ports: WatchPorts) -> None:
        self._watch_ports = ports

    async def _run_watch_observer(
        self,
        task_id: str | None,
        observer_id: str,
        input_value: Mapping[str, Any],
        workspace,
        *,
        profile=None,
        task_policy=None,
        task_budget=None,
    ) -> dict[str, Any]:
        dispatcher = self._watch_ports.dispatcher
        if dispatcher is None:
            return {"status": "failed", "error": "dispatcher unavailable"}
        result = await dispatcher.dispatch(
            CapabilityRequest(
                capability_id=observer_id,
                arguments={"input": dict(input_value)},
                task_id=task_id,
                call_id=new_id("watch-observer"),
                origin=CapabilityRequestOrigin.GENERATED,
            ),
            workspace=workspace,
            profile=profile,
            task_policy=task_policy,
            task_budget=task_budget,
        )
        if not isinstance(result, CapabilityResult):
            return {"status": "failed", "error": "observer call suspended"}
        if result.status is not CapabilityResultStatus.OK:
            return {
                "status": getattr(result.status, "value", "failed"),
                "error": getattr(result, "error", None) or "observer failed",
            }
        try:
            value = json.loads(result.output or "null")
        except (TypeError, ValueError):
            value = result.output
        return {
            "status": "ok",
            "observer_id": observer_id,
            "value": value,
            "proof": dict(getattr(result, "metadata", {}) or {}),
        }

    async def _poll_watches(self) -> None:
        """Background poll of registered watches -> WatchObserved events."""
        events = None
        while True:
            await asyncio.sleep(2.0)
            registry = self._watch_ports.watch_registry
            if registry is None or not (registry.file_watches or registry.process_watches):
                continue
            if events is None:
                try:
                    events = self._watch_ports.require_events()
                except Exception as exc:
                    registry.record_poll_error(exc)
                    continue
            event_store = events

            async def sink(type_, payload, task_id=None, event_store=event_store):
                if type_ == "WatchObserved":
                    await self._invalidate_watch_claims(payload)
                await event_store.append_event(type_, payload, task_id=task_id)

            try:
                await registry.poll_all(sink)
            except Exception as exc:
                registry.record_poll_error(exc)
                _logger.warning("watch poll error: %s", exc)
            else:
                if not registry.poll_had_error:
                    registry.record_poll_success()

    async def _invalidate_watch_claims(self, payload: Mapping[str, Any]) -> None:
        raw_changes = payload.get("changes")
        if isinstance(raw_changes, list):
            changes = [str(item).removesuffix(" (removed)") for item in raw_changes if str(item)]
        else:
            changes = []
        if payload.get("kind") == "process":
            changes = ["*"]
        if not changes:
            return
        root = os.path.realpath(str(payload.get("root") or ""))
        default_workspace = self._watch_ports.default_workspace
        workspace_root = os.path.realpath(str(getattr(default_workspace, "root", "")))
        if root and workspace_root and root != workspace_root:
            if root.startswith(workspace_root + os.sep):
                prefix = os.path.relpath(root, workspace_root)
                changes = [os.path.join(prefix, path) for path in changes]
            else:
                return
        cache = self._watch_ports.world_states or {}
        for world_state in list(cache.values()):
            world_state.claims.invalidate_for_paths(changes)
        index_coordinator = self._watch_ports.project_index_coordinator
        if index_coordinator is not None:
            absolute_changes = [
                os.path.join(root or workspace_root, path)
                if path != "*"
                else (root or workspace_root)
                for path in changes
            ]
            index_coordinator.mark_stale_for_paths(absolute_changes)
        store = self._watch_ports.world_state_store
        if store is not None:
            try:
                await store.invalidate_for_paths(None, changes)
            except Exception as exc:
                _logger.warning("durable watch claim invalidation failed: %s", exc)

    async def _cleanup_task_watches(self, task, result) -> None:
        registry = self._watch_ports.watch_registry
        if registry is not None:
            registry.remove_task(getattr(task, "id", None))


__all__ = ["ServiceWatchAPI"]
