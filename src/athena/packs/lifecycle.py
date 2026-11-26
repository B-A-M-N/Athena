"""Public lifecycle coordinator for pack installer, activator, and hook runtimes."""

from __future__ import annotations

from typing import Any


class PackLifecycleService:
    """Compose the pack lifecycle components behind one stable application port."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    def bind_integrations(self, **kwargs: Any) -> None:
        self._manager.bind_integrations(**kwargs)

    async def rehydrate_enabled(self) -> int:
        return await self._manager.rehydrate_enabled()

    async def install(self, source_path: str, **kwargs: Any):
        return await self._manager._installer.install(source_path, **kwargs)

    async def install_remote(self, source_url: str, **kwargs: Any):
        return await self._manager.install_remote(source_url, **kwargs)

    async def upgrade(self, source_path: str, **kwargs: Any):
        return await self._manager._installer.upgrade(source_path, **kwargs)

    async def enable(self, pack_id: str):
        return await self._manager._installer.enable(pack_id)

    async def disable(self, pack_id: str):
        return await self._manager._installer.disable(pack_id)

    async def uninstall(self, pack_id: str) -> bool:
        return await self._manager._installer.uninstall(pack_id)

    async def replay_hook_outbox(self) -> int:
        return await self._manager._hooks.replay()

    async def start_hook_dispatcher(self, interval_s: float = 1.0) -> None:
        await self._manager._hooks.start(interval_s=interval_s)

    async def stop_hook_dispatcher(self) -> None:
        await self._manager._hooks.stop()

    def rehydration_failures(self):
        return self._manager.rehydration_failures()


__all__ = ["PackLifecycleService"]
