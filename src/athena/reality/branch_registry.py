"""Active and ephemeral reality-branch lifecycle beneath ``RealityGate``."""

from __future__ import annotations

from typing import Any

__all__ = ["RealityBranchRegistry"]


class RealityBranchRegistry:
    """Own branch identity maps without routing or mutation authority."""

    def __init__(self, shadow_engine: Any) -> None:
        self._shadow = shadow_engine
        self.active: dict[str, Any] = {}
        self.ephemeral: dict[str, Any] = {}

    def rehydrate(self) -> None:
        """Restore only durable proposed/executing/verified candidate branches."""
        list_branches = getattr(self._shadow, "list_branches", None)
        if list_branches is None:
            return
        for branch in list_branches():
            if getattr(branch, "status", None) not in {"PROPOSED", "EXECUTING", "VERIFIED"}:
                continue
            task_id = getattr(branch, "task_id", None)
            if task_id:
                self.active[task_id] = branch

    def active_branch(self, task_id: str | None) -> Any | None:
        return self.active.get(task_id) if task_id else None

    def activate(self, branch: Any) -> None:
        task_id = getattr(branch, "task_id", None)
        if task_id:
            self.active[task_id] = branch

    def active_branches(self) -> tuple[Any, ...]:
        return tuple(self.active.values())

    def ephemeral_branch(self, call_id: str | None) -> Any | None:
        return self.ephemeral.get(call_id) if call_id else None

    def set_ephemeral(self, call_id: str, branch: Any) -> None:
        self.ephemeral[call_id] = branch

    async def discard_ephemeral(self, call_id: str | None) -> None:
        branch = self.ephemeral.get(call_id) if call_id else None
        if branch is None:
            return
        try:
            await self._shadow.discard(branch, reason="isolated call complete")
        except Exception:  # noqa: BLE001 - best-effort isolated cleanup
            return
        if call_id is not None and self.ephemeral.get(call_id) is branch:
            self.ephemeral.pop(call_id, None)

    def deactivate(self, task_id: str | None) -> None:
        if task_id is not None:
            self.active.pop(task_id, None)
