"""Result observations, health persistence, failure memory, and cache lookup."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from athena.capabilities.dispatch_helpers import _cache_ttl, bounded_diagnostics
from athena.protocol.capabilities import CapabilityRequest, CapabilityResult, CapabilityResultStatus
from athena.protocol.events import EV

if TYPE_CHECKING:
    from athena.capabilities.dispatcher import CapabilityDispatcher


class DispatchResultCache:
    """Own result-cache mechanics without owning dispatch decisions."""

    def __init__(self, dispatcher: "CapabilityDispatcher") -> None:
        self._d = dispatcher

    async def persist_health(self, capability_id: str) -> None:
        persist = getattr(self._d._health, "persist", None)
        if persist is None:
            return
        try:
            await persist(capability_id)
            mark_persisted = getattr(self._d._health, "mark_persisted", None)
            if callable(mark_persisted):
                mark_persisted(capability_id)
        except Exception as exc:  # health persistence must not alter call truth
            await self._d._emit(
                "CapabilityHealthChanged",
                {
                    "capability_id": capability_id,
                    "persistence_error": str(exc)[:500],
                },
                None,
                causal_id=capability_id,
            )

    def health_should_persist(self, capability_id: str, state_changed: bool) -> bool:
        should_persist = getattr(self._d._health, "should_persist", None)
        if callable(should_persist):
            return bool(should_persist(capability_id, state_changed=state_changed))
        return True

    async def emit_result_observations(
        self,
        request: CapabilityRequest,
        result: CapabilityResult,
    ) -> None:
        """Publish structured result evidence for projections and replay."""
        diagnostics = (result.metadata or {}).get("diagnostics")
        if not isinstance(diagnostics, (list, tuple)) or not diagnostics:
            return
        await self._d._emit(
            EV["DIAGNOSTICS_PRODUCED"],
            {
                "call_id": request.call_id,
                "capability_id": request.capability_id,
                "diagnostics": bounded_diagnostics(diagnostics),
                "count": len(diagnostics),
            },
            request.task_id,
            causal_id=request.call_id,
        )

    async def attach_failure_memory(self, request: CapabilityRequest, result, workspace) -> None:
        """Attach advisory deterministic repair history to failed results."""
        if self._d._failure_memory is None or result.status is CapabilityResultStatus.OK:
            return
        diagnostics = (result.metadata or {}).get("diagnostics")
        if not isinstance(diagnostics, (list, tuple)):
            return
        suggestions: list[dict[str, Any]] = []
        environment = str((result.metadata or {}).get("failure_environment_fingerprint") or "")
        for item in diagnostics[:8]:
            if not isinstance(item, Mapping):
                continue
            signature = str(item.get("signature_fingerprint") or item.get("fingerprint") or "")
            if not signature:
                continue
            try:
                suggestions.extend(
                    await self._d._failure_memory.retrieve(
                        signature_fingerprint=signature,
                        capability_id=request.capability_id,
                        environment_fingerprint=environment,
                        project_scope=getattr(workspace, "id", None),
                        limit=4,
                    )
                )
            except Exception:
                continue
        if suggestions:
            metadata = dict(result.metadata or {})
            metadata["failure_memory"] = suggestions[:8]
            object.__setattr__(result, "metadata", metadata)

    def cached_result(self, key: tuple[str | None, str, str, str, str | None]):
        entry = self._d._result_cache.get(key)
        if entry is None:
            return None
        expires_at, result = entry
        if time.monotonic() >= expires_at:
            self._d._result_cache.pop(key, None)
            return None
        self._d._result_cache.move_to_end(key)
        return result

    def invalidate(self, workspace_root: str) -> None:
        root = os.path.realpath(os.path.abspath(workspace_root))
        for key in list(self._d._result_cache):
            if key[1] == root:
                self._d._result_cache.pop(key, None)

    def store_success(self, key, result, descriptor, arguments: Mapping[str, Any]) -> None:
        policy = descriptor.resolve_cache_policy(arguments)
        self._d._result_cache[key] = (
            time.monotonic() + _cache_ttl(descriptor, policy),
            result,
        )
        self._d._result_cache.move_to_end(key)
        while len(self._d._result_cache) > self._d._result_cache_limit:
            self._d._result_cache.popitem(last=False)


__all__ = ["DispatchResultCache"]
