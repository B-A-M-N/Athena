"""Durable event-hook runtime for declarative packs."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any, Mapping

from athena.packs.models import PackState
from athena.packs.ports import PackLegacyHookSlots, PackHookPorts
from athena.packs.runtime_state import PackHookRuntimeState
from athena.protocol.messages import utcnow

_logger = logging.getLogger("athena.packs.hooks")


class PackHookRuntime:
    """Own callback registration, replay, retry, and hook health."""

    def __init__(self, ports: PackHookPorts | Any) -> None:
        """Accept explicit ports (preferred) or a legacy host for compatibility.

        Shared mutable hook state lives in :class:`PackHookRuntimeState`; a
        legacy host is accepted only until every composition is migrated.
        """
        if not isinstance(ports, PackHookPorts):
            raise TypeError("PackHookRuntime requires explicit PackHookPorts")
        self._state = ports.state if isinstance(ports.state, PackHookRuntimeState) else None
        self._host = ports
        self._legacy_slots = PackLegacyHookSlots(
            hook_retry_task=(
                ports.hook_retry_task()
                if callable(ports.hook_retry_task)
                else ports.hook_retry_task
            )
        )

    @property
    def ports(self) -> PackHookPorts:
        """Return the explicit runtime ports."""
        return self._host

    @ports.setter
    def ports(self, ports: PackHookPorts) -> None:
        if not isinstance(ports, PackHookPorts):
            raise TypeError("PackHookRuntime requires explicit PackHookPorts")
        self._state = ports.state if isinstance(ports.state, PackHookRuntimeState) else None
        self._host = ports
        self._legacy_slots = PackLegacyHookSlots(
            hook_retry_task=(
                ports.hook_retry_task()
                if callable(ports.hook_retry_task)
                else ports.hook_retry_task
            )
        )

    @property
    def _hooks(self) -> PackHookRuntimeState | None:
        return self._state

    @property
    def _legacy(self) -> Any:
        """Legacy compatibility host; PackHookPorts fields are accessed directly."""
        return self._host

    def _legacy_hook_health(self) -> Any:
        return self._host.hook_health

    def _legacy_field(self, port_name: str, legacy_name: str) -> Any:
        return getattr(self._host, legacy_name, None) or getattr(self._host, port_name)

    def health(self) -> dict[str, Any]:
        if self._state is not None:
            return dict(self._state.health)
        return dict(self._legacy_hook_health())

    async def resolve_workflow(self, state: PackState, workflow_id: str) -> str:
        workflow_store = self._legacy_field("workflow_store", "_workflow_store")
        if workflow_store is None:
            return workflow_id
        for workflow in await workflow_store.list():
            provenance = dict(workflow.provenance or {})
            if provenance.get("pack_id") != state.id:
                continue
            if workflow.id == workflow_id or provenance.get("source_id") == workflow_id:
                return workflow.id
        raise ValueError(
            f"pack hook workflow {workflow_id!r} is not an active workflow from pack {state.id!r}"
        )

    async def replay(self) -> int:
        outbox = (
            self._legacy_field("hook_outbox", "_hook_outbox")
            if self._state is None
            else self._state.outbox
        )
        callbacks_source = (
            self._state.callbacks
            if self._state is not None
            else self._legacy_field("hook_callbacks", "_hook_callbacks")
        )
        if outbox is None:
            return 0
        callbacks = {
            hook_id: callback
            for values in callbacks_source.values()
            for hook_id, callback in values
        }
        replayed = 0
        for row in await outbox.pending():
            callback = callbacks.get(str(row.get("hook_id") or ""))
            if callback is None:
                continue
            claimed = await outbox.claim(str(row.get("id") or ""))
            if claimed is None:
                continue
            row = claimed
            try:
                contracts = (
                    self._state.contracts
                    if self._state is not None
                    else self._legacy_field("hook_contracts", "_hook_contracts")
                )
                contract = contracts.get(str(row.get("hook_id") or ""))
                if contract is None or row.get("hook_contract_digest") != contract["digest"]:
                    mark_stale = getattr(outbox, "mark_stale_contract", None)
                    if mark_stale is not None:
                        await mark_stale(
                            str(row.get("id") or ""),
                            (
                                "installed Pack no longer has the persisted hook contract"
                                if contract is None
                                else "installed Pack no longer matches the persisted hook contract"
                            ),
                            claim_token=row.get("claim_token"),
                        )
                    continue
                payload = json.loads(str(row.get("payload") or "{}"))
                event = SimpleNamespace(
                    id=str(row.get("event_id") or ""),
                    type=str(row.get("event_type") or ""),
                    task_id=row.get("task_id"),
                    session_id=row.get("session_id"),
                    payload=payload if isinstance(payload, Mapping) else {},
                    _hook_outbox_row=row,
                )
                await callback(event)
            except Exception as exc:  # noqa: BLE001 - recovery remains retryable
                kwargs = {"claim_token": row.get("claim_token")} if row.get("claim_token") else {}
                await outbox.mark_failed(str(row.get("id") or ""), str(exc), **kwargs)
            else:
                replayed += 1
        return replayed

    async def start(self, interval_s: float = 1.0) -> None:
        retry_task = (
            self._state.retry_task
            if self._state is not None
            else self._legacy_slots.hook_retry_task
        )
        outbox = (
            self._legacy_field("hook_outbox", "_hook_outbox")
            if self._state is None
            else self._state.outbox
        )
        health = self._state.health if self._state is not None else self._legacy_hook_health()
        if retry_task is not None or outbox is None:
            return

        async def _loop() -> None:
            health["state"] = "running"
            while True:
                try:
                    health["iterations"] = int(health.get("iterations", 0)) + 1
                    await self.replay()
                    health["last_success_at"] = utcnow().isoformat()
                    health["last_error"] = None
                    await asyncio.sleep(max(0.1, float(interval_s)))
                except asyncio.CancelledError:
                    health["state"] = "stopped"
                    raise
                except Exception as exc:  # noqa: BLE001 - one retry cannot stop the dispatcher
                    health.update(
                        state="degraded",
                        last_error_at=utcnow().isoformat(),
                        last_error=str(exc)[:2000],
                    )
                    _logger.warning("pack hook retry iteration failed: %s", exc)
                    await asyncio.sleep(min(30.0, max(0.25, float(interval_s))))

        new_task = asyncio.create_task(_loop())
        if self._state is not None:
            self._state.retry_task = new_task
        else:
            self._legacy_slots.hook_retry_task = new_task

    async def stop(self) -> None:
        task = (
            self._state.retry_task
            if self._state is not None
            else self._legacy_slots.hook_retry_task
        )
        if self._state is not None:
            self._state.retry_task = None
        else:
            self._legacy_slots.hook_retry_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


__all__ = ["PackHookRuntime"]
