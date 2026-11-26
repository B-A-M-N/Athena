"""Install and durable state transitions for declarative packs."""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from athena.packs.models import PackState
from athena.packs.ports import PackInstallPorts

from athena.concurrency import run_blocking


class PackInstaller:
    """Own copy, upgrade, enable, disable, and uninstall transactions."""

    def __init__(self, ports: PackInstallPorts) -> None:
        """Receive exactly the install capabilities this mechanism needs."""
        self.ports = ports

    async def install(
        self,
        source_path: str,
        *,
        allowed_root: str | None = None,
        enable: bool = True,
        provenance: Mapping[str, Any] | None = None,
    ) -> PackState:
        source, manifest, integrity = self.ports.validated_source(
            source_path, allowed_root=allowed_root
        )
        target = self.ports.root / manifest.id / manifest.version
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing = await self.ports.store.get(manifest.id)
            if existing and existing.source_integrity == integrity:
                return (
                    await self.enable(manifest.id) if enable and not existing.enabled else existing
                )
            raise ValueError(f"pack version already installed: {manifest.id}@{manifest.version}")
        temp = Path(tempfile.mkdtemp(prefix=".pack-", dir=str(target.parent)))
        try:
            await run_blocking(shutil.copytree, source, temp / "payload", symlinks=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            await run_blocking(os.replace, temp / "payload", target)
        finally:
            await run_blocking(shutil.rmtree, temp, ignore_errors=True)
        state = PackState(
            manifest=manifest,
            install_path=str(target),
            enabled=enable,
            installed_at=datetime.now(timezone.utc).isoformat(),
            source_integrity=integrity,
            health="healthy",
            provenance=dict(provenance or {"kind": "local_pack_source"}),
        )
        await self.ports.store.save(state)
        if enable and self.ports.integrations_bound:
            try:
                await self.ports.activate(state)
            except Exception:  # noqa: BLE001 - durable enable follows live admission
                await self.ports.store.set_enabled(manifest.id, False)
                raise
        return state

    async def install_remote(
        self,
        source_url: str,
        *,
        expected_sha256: str | None = None,
        expected_sha256_source: str | None = None,
        approved: bool = False,
        enable: bool = True,
        network_policy: str | object | None = None,
    ) -> PackState:
        if not approved:
            raise PermissionError("remote pack installation requires explicit operator approval")
        fetched = await run_blocking(
            self.ports.fetch_remote,
            source_url,
            expected_sha256=expected_sha256,
            expected_sha256_source=expected_sha256_source,
            network_policy=network_policy,
        )
        provenance = dict(fetched["provenance"]) | dict(fetched["authenticity"])
        return await self.install(fetched["source_path"], enable=enable, provenance=provenance)

    async def upgrade(self, source_path: str, *, allowed_root: str | None = None) -> PackState:
        _source, manifest, _integrity = self.ports.validated_source(
            source_path, allowed_root=allowed_root
        )
        prior = await self.ports.store.get(manifest.id)
        if prior is not None and self.ports.integrations_bound:
            hooks_suspended = False
            if self.ports.hook_outbox is not None:
                await self.ports.hook_outbox.suspend_pack(manifest.id, "pack upgrade in progress")
                hooks_suspended = True

            async def resume_hooks() -> None:
                nonlocal hooks_suspended
                if hooks_suspended and self.ports.hook_outbox is not None:
                    await self.ports.hook_outbox.resume_pack(manifest.id)
                    hooks_suspended = False

            try:
                state = await self.install(source_path, allowed_root=allowed_root, enable=False)
                await self.ports.deactivate(prior, remove=True)
                enabled = await self.ports.store.set_enabled(manifest.id, True)
                if enabled is None:
                    raise RuntimeError(f"upgraded pack disappeared: {manifest.id}")
                try:
                    await self.ports.activate(enabled)
                except Exception:
                    await self.ports.store.set_enabled(manifest.id, False)
                    try:
                        await self.ports.store.save(prior)
                        await self.ports.activate(prior)
                    except Exception as restore_error:
                        raise RuntimeError(
                            "pack upgrade failed and prior version could not be restored: "
                            f"{restore_error}"
                        ) from restore_error
                    raise
                self.ports.remove_installed_path(prior.install_path)
                return enabled
            finally:
                await resume_hooks()
        state = await self.install(source_path, allowed_root=allowed_root, enable=True)
        if prior is not None and prior.manifest.version != state.manifest.version:
            await self.ports.store.set_enabled(prior.id, False)
            self.ports.remove_installed_path(prior.install_path)
        return state

    async def enable(self, pack_id: str) -> PackState:
        state = await self.ports.store.set_enabled(pack_id, True)
        if state is None:
            raise KeyError(f"pack not found: {pack_id}")
        health = self.ports.health(state)
        if health["status"] != "healthy":
            await self.ports.store.set_enabled(pack_id, False)
            raise ValueError(f"pack integrity check failed: {pack_id}")
        if self.ports.integrations_bound:
            try:
                await self.ports.activate(state)
            except Exception:
                await self.ports.store.set_enabled(pack_id, False)
                raise
        if self.ports.hook_outbox is not None:
            await self.ports.hook_outbox.resume_pack(pack_id)
        return state

    async def disable(self, pack_id: str) -> PackState:
        state = await self.ports.store.set_enabled(pack_id, False)
        if state is None:
            raise KeyError(f"pack not found: {pack_id}")
        if self.ports.integrations_bound:
            await self.ports.deactivate(state, remove=False)
        if self.ports.hook_outbox is not None:
            await self.ports.hook_outbox.suspend_pack(pack_id)
        return state

    async def uninstall(self, pack_id: str) -> bool:
        state = await self.ports.store.get(pack_id)
        if state is None:
            return False
        if self.ports.integrations_bound:
            await self.ports.deactivate(state, remove=True)
        if self.ports.hook_outbox is not None:
            await self.ports.hook_outbox.cancel_pack(pack_id)
        self.ports.remove_installed_path(state.install_path, pack_id=pack_id)
        return await self.ports.store.delete(pack_id)


__all__ = ["PackInstaller"]
