"""Operator-facing pack lifecycle mechanics subordinate to ``AthenaService``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

__all__ = ["PackAPI"]


class PackAPI:
    """Expose pack operations through explicit manager/configuration ports."""

    def __init__(
        self,
        *,
        manager: Callable[[], Any],
        workspace_root: Callable[[], str | None],
    ) -> None:
        self._manager = manager
        self._workspace_root = workspace_root

    async def list(self, query: str | None = None) -> list[dict[str, Any]]:
        manager = self._manager()
        if manager is None:
            return []
        rows = list(await manager.list())
        if query:
            needle = query.casefold()
            rows = [
                row
                for row in rows
                if needle in str(row.get("id", "")).casefold()
                or needle in str(row.get("publisher", "")).casefold()
            ]
        for row in rows:
            health = await manager.health_for(str(row["id"]))
            if health is not None:
                row["health_detail"] = health
        return rows

    async def inspect(self, pack_id: str) -> dict[str, Any] | None:
        manager = self._manager()
        if manager is None:
            return None
        try:
            return await manager.inspect_installed(pack_id)
        except KeyError:
            return None

    async def install(self, source_path: str, *, enable: bool = True) -> dict[str, Any]:
        manager = self._manager()
        if manager is None:
            raise RuntimeError("pack manager is unavailable")
        state = await manager.lifecycle.install(
            source_path,
            allowed_root=self._workspace_root(),
            enable=enable,
        )
        return state.to_record()

    async def enable(self, pack_id: str) -> dict[str, Any]:
        manager = self._manager()
        if manager is None:
            raise RuntimeError("pack manager is unavailable")
        return (await manager.lifecycle.enable(pack_id)).to_record()

    async def disable(self, pack_id: str) -> dict[str, Any]:
        manager = self._manager()
        if manager is None:
            raise RuntimeError("pack manager is unavailable")
        return (await manager.lifecycle.disable(pack_id)).to_record()

    async def remove(self, pack_id: str) -> bool:
        manager = self._manager()
        if manager is None:
            return False
        return await manager.lifecycle.uninstall(pack_id)
