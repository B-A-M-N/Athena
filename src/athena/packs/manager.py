"""Validation and lifecycle for local declarative packs."""

from __future__ import annotations

from importlib import import_module
import json
import logging
import os
import re
import shutil
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Mapping

from athena.packs.models import PackState
from athena.packs.runtime_state import PackRuntimeState
from athena.packs.ports import (
    PackActivationPorts,
    PackHookPorts,
    PackInstallPorts,
    PackMutableActivationState,
)
from athena.packs.hooks import PackHookRuntime
from athena.packs.activation import PackActivator
from athena.packs.install import PackInstaller
from athena.packs.provenance import index_provenance, remote_archive_receipts
from athena.mcp.client import MCPClient
from athena.network import (
    classify_endpoint,
    validate_endpoint,
)
from athena.packs.source import (
    directory_integrity as _directory_integrity,
    download_remote as _download_remote,
    extract_archive_safely as _extract_archive_safely,
    file_sha256 as _file_sha256,
    find_pack_root as _find_pack_root,
    govern_remote_target as _govern_remote_target,
    inside as _inside,
    parse_manifest as _parse_manifest,
    tree_size as _tree_size,
    validated_source_for_remote as _validated_source_for_remote,
    validate_provided_files as _validate_provided_files,
)

try:
    tomllib = import_module("tomllib")
except ModuleNotFoundError:  # pragma: no cover - legacy/minimal Python builds
    tomllib = import_module("tomli")


_REMOTE_CONTENT_CACHE_LIMIT = 512 * 1024 * 1024
_REMOTE_CONTENT_CACHE_TTL = 7 * 24 * 60 * 60

_logger = logging.getLogger("athena.packs")


class PackManager:
    """Manage packs without loading pack code into Athena's interpreter."""

    def __init__(self, store, *, install_root: str) -> None:
        self._store = store
        self._root = Path(install_root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._content_root = self._root / ".content"
        self._skill_lifecycle = None
        self._workflow_store = None
        self._fabric = None
        self._dispatcher = None
        self._mcp_adapter = None
        self._mcp_client_sink = None
        self._mcp_clients: dict[str, MCPClient] = {}
        self._rehydration_failures: list[dict[str, str]] = []
        self._event_store = None
        self._task_intake = None
        self._task_lookup = None
        self._workspace = None
        self._runtime_state = PackRuntimeState()
        self._activation_state = PackMutableActivationState()
        hooks = self._runtime_state.hooks
        self._hook_callbacks = hooks.callbacks
        self._hook_contracts = hooks.contracts
        self._hook_events_seen = hooks.events_seen
        self._hook_outbox = hooks.outbox
        self._hook_retry_task = hooks.retry_task
        self._hook_health = hooks.health
        # Extracted mechanisms take explicit typed ports; the port
        # dataclasses name every capability they consume instead of reaching
        # through a host reference.
        self._hooks = PackHookRuntime(self._hook_ports())
        self._activator = PackActivator(self._activation_ports())
        self._installer = PackInstaller(self._install_ports())
        self._lifecycle: Optional[Any] = None

    @property
    def lifecycle(self):
        """Return the extracted lifecycle surface with compatibility intact."""
        if self._lifecycle is None:
            from athena.packs.lifecycle import PackLifecycleService

            self._lifecycle = PackLifecycleService(self)
        return self._lifecycle

    def _hook_ports(self) -> "PackHookPorts":
        return PackHookPorts(
            hook_callbacks=self._hook_callbacks,
            hook_contracts=self._hook_contracts,
            hook_health=self._hook_health,
            hook_outbox=self._hook_outbox,
            hook_retry_task=lambda: self._hook_retry_task,
            workflow_store=self._workflow_store,
            state=self._runtime_state.hooks,
        )

    def _activation_ports(self) -> "PackActivationPorts":
        return PackActivationPorts(
            contributions=self._contributions,
            save_contribution=self._save_contribution,
            delete_contributions=self._delete_contributions,
            activate_skills=self._activate_skills,
            activate_workflows=self._activate_workflows,
            activate_capabilities=self._activate_capabilities,
            activate_instruments=self._activate_instruments,
            activate_mcp_servers=self._activate_mcp_servers,
            activate_hooks=self._activate_hooks,
            workflow_store=self._workflow_store,
            skill_lifecycle=self._skill_lifecycle,
            fabric=self._fabric,
            event_store=self._event_store,
            hook_callbacks=self._hook_callbacks,
            hook_contracts=self._hook_contracts,
            mcp_adapter=self._mcp_adapter,
            mutable=self._activation_state,
        )

    def _install_ports(self) -> "PackInstallPorts":
        return PackInstallPorts(
            store=self._store,
            validated_source=self._validated_source,
            root=self._root,
            integrations_bound=self._integrations_bound,
            hook_outbox=self._hook_outbox,
            remove_installed_path=self._remove_installed_path,
            activate=self._activate,
            deactivate=self._deactivate,
            health=self.health,
            fetch_remote=self.fetch_remote,
        )

    def bind_integrations(
        self,
        *,
        skill_lifecycle=None,
        workflow_store=None,
        fabric=None,
        dispatcher=None,
        mcp_adapter=None,
        mcp_client_sink=None,
        event_store=None,
        hook_outbox=None,
        task_intake=None,
        task_lookup=None,
        workspace=None,
    ) -> None:
        """Bind live surfaces that declarative pack contributions may enter.

        Packs remain data-only. This method gives the manager the existing
        skill/workflow stores and capability registry; it never imports a
        module from a pack.
        """
        self._skill_lifecycle = skill_lifecycle
        self._workflow_store = workflow_store
        self._fabric = fabric
        self._dispatcher = dispatcher
        self._mcp_adapter = mcp_adapter
        self._mcp_client_sink = mcp_client_sink
        self._event_store = event_store
        self._hook_outbox = hook_outbox
        self._runtime_state.hooks.outbox = hook_outbox
        self._task_intake = task_intake
        self._task_lookup = task_lookup
        self._workspace = workspace
        self._hooks.ports = self._hook_ports()
        self._activator.ports = self._activation_ports()
        self._installer.ports = self._install_ports()

    async def rehydrate_enabled(self) -> int:
        """Activate enabled packs after the host rebuilds its surfaces."""
        if not self._integrations_bound:
            return 0
        self._rehydration_failures = []
        activated = 0
        for state in await self._store.list():
            if not state.enabled:
                continue
            try:
                await self._activate(state)
            except Exception as exc:  # noqa: BLE001 - optional packs degrade independently
                _logger.warning("capability-pack rehydration failed for %s: %s", state.id, exc)
                self._rehydration_failures.append({"pack_id": state.id, "error": str(exc)})
                continue
            activated += 1
        return activated

    def rehydration_failures(self) -> list[dict[str, str]]:
        """Return pack failures from the most recent startup rehydration."""
        return [dict(item) for item in self._rehydration_failures]

    def hook_dispatch_health(self) -> dict[str, Any]:
        return self._hooks.health()

    async def health_for(self, pack_id: str) -> dict[str, Any] | None:
        """Return one installed pack's health through the public manager API."""
        state = await self._store.get(str(pack_id))
        return self.health(state) if state is not None else None

    async def replay_hook_outbox(self) -> int:
        """Replay hook deliveries that were durable before a process restart."""
        return await self._hooks.replay()

    async def start_hook_dispatcher(self, interval_s: float = 1.0) -> None:
        """Keep durable hook retries moving after startup replay."""
        await self._hooks.start(interval_s=interval_s)

    async def stop_hook_dispatcher(self) -> None:
        await self._hooks.stop()

    async def _resolve_hook_workflow(self, state: PackState, workflow_id: str) -> str:
        return await self._hooks.resolve_workflow(state, workflow_id)

    def inspect_source(
        self, source_path: str, *, allowed_root: str | None = None
    ) -> dict[str, Any]:
        source, manifest, integrity = self._validated_source(source_path, allowed_root=allowed_root)
        return {
            "kind": "capability_pack",
            "source_path": str(source),
            "manifest": manifest.to_record(computed_integrity=integrity),
            "valid": True,
            "executable_code_loaded": False,
        }

    def fetch_remote(
        self,
        source_url: str,
        *,
        expected_sha256: str | None = None,
        expected_sha256_source: str | None = None,
        max_bytes: int = 32 * 1024 * 1024,
        network_policy: str | object | None = None,
    ) -> dict[str, Any]:
        """Fetch one archive into a content-addressed quarantine directory.

        Remote bytes are never activated directly. Archives must be zip/tar,
        contain no links or path escapes, and pass declarative validation.
        """
        parsed = urllib.parse.urlparse(str(source_url))
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError("remote pack source must be an http(s) URL")
        endpoint = validate_endpoint(
            str(source_url),
            credentialed=False,
            allow_insecure_remote=classify_endpoint(str(source_url)) == "loopback",
        )
        if endpoint.scheme != "https" and not endpoint.loopback:
            raise ValueError("non-loopback remote packs require HTTPS")
        target = _govern_remote_target(str(source_url), network_policy)
        expected = str(expected_sha256 or "").lower()
        if expected and not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("expected_sha256 must be a 64-character hex digest")
        if not expected and not endpoint.loopback:
            raise ValueError(
                "non-loopback remote packs require an operator-provided expected_sha256"
            )
        quarantine = Path(tempfile.mkdtemp(prefix=".pack-quarantine-", dir=str(self._root)))
        archive = quarantine / "source.archive"
        try:
            _download_remote(
                str(source_url),
                target=target,
                max_bytes=max_bytes,
                timeout=20.0,
                user_agent="athena-pack-fetch/1",
                destination=archive,
            )
            archive_hash = _file_sha256(archive)
            if expected and archive_hash != expected:
                raise ValueError("remote pack archive hash does not match expected_sha256")
            content_root = self._content_root / archive_hash
            if not content_root.exists():
                extract_root = quarantine / "payload"
                extract_root.mkdir()
                _extract_archive_safely(archive, extract_root)
                source_root = _find_pack_root(extract_root)
                _validated_source_for_remote(source_root)
                incoming_size = _tree_size(source_root)
                if incoming_size > _REMOTE_CONTENT_CACHE_LIMIT:
                    raise ValueError("remote pack exceeds the content cache limit")
                self._prune_content_cache(reserve_bytes=incoming_size)
                content_root.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source_root, content_root)
            else:
                # A cache hit is live use; refresh its TTL before returning it.
                os.utime(content_root, None)
            source, manifest, integrity = self._validated_source(str(content_root))
            provenance, authenticity = remote_archive_receipts(
                str(source_url), endpoint, archive_hash, expected or None, expected_sha256_source
            )  # noqa: E501
            return {
                "source_path": str(source),
                "archive_sha256": archive_hash,
                "pack_integrity": integrity,
                "manifest": manifest.to_record(computed_integrity=integrity),
                "quarantined": True,
                "operator_approval_required": True,
                "provenance": provenance,
                "authenticity": authenticity,
            }
        finally:
            shutil.rmtree(quarantine, ignore_errors=True)

    def _prune_content_cache(self, *, reserve_bytes: int = 0) -> None:
        """Bound remote approval material so abandoned fetches cannot grow forever."""
        if not self._content_root.exists():
            return
        now = datetime.now(timezone.utc).timestamp()
        entries: list[tuple[float, int, Path]] = []
        for entry in self._content_root.iterdir():
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            try:
                mtime = entry.stat().st_mtime
                size = sum(item.stat().st_size for item in entry.rglob("*") if item.is_file())
            except OSError:
                continue
            if now - mtime > _REMOTE_CONTENT_CACHE_TTL:
                shutil.rmtree(entry, ignore_errors=True)
                continue
            entries.append((mtime, size, entry))
        total = sum(size for _mtime, size, _entry in entries)
        target = max(0, _REMOTE_CONTENT_CACHE_LIMIT - max(0, int(reserve_bytes)))
        for _, size, entry in sorted(entries):
            if total <= target:
                break
            shutil.rmtree(entry, ignore_errors=True)
            total -= size

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
        return await self._installer.install_remote(
            source_url,
            expected_sha256=expected_sha256,
            expected_sha256_source=expected_sha256_source,
            approved=approved,
            enable=enable,
            network_policy=network_policy,
        )

    def search_remote(
        self,
        source_url: str,
        *,
        query: str = "",
        network_policy: str | object | None = None,
    ) -> list[dict[str, Any]]:
        """Search an operator-configured JSON pack index; metadata is untrusted."""
        parsed = urllib.parse.urlparse(str(source_url))
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError("remote pack source must be an http(s) URL")
        endpoint = validate_endpoint(
            str(source_url),
            credentialed=False,
            allow_insecure_remote=classify_endpoint(str(source_url)) == "loopback",
        )
        if endpoint.scheme != "https" and not endpoint.loopback:
            raise ValueError("non-loopback remote pack indexes require HTTPS")
        target = _govern_remote_target(str(source_url), network_policy)
        raw = json.loads(
            _download_remote(
                str(source_url),
                target=target,
                max_bytes=4 * 1024 * 1024,
                timeout=10.0,
                user_agent="athena-pack-search/1",
            )
        )
        records = raw.get("packs", raw) if isinstance(raw, Mapping) else raw
        if not isinstance(records, list):
            raise ValueError("remote pack index must contain an array")
        needle = str(query or "").casefold()
        return [
            {
                **dict(item),
                "provenance": index_provenance(str(source_url), endpoint),
            }
            for item in records[:500]
            if isinstance(item, Mapping)
            and (not needle or needle in json.dumps(item, sort_keys=True).casefold())
        ]

    async def install(
        self,
        source_path: str,
        *,
        allowed_root: str | None = None,
        enable: bool = True,
        provenance: Mapping[str, Any] | None = None,
    ) -> PackState:
        return await self._installer.install(
            source_path,
            allowed_root=allowed_root,
            enable=enable,
            provenance=provenance,
        )

    async def upgrade(self, source_path: str, *, allowed_root: str | None = None) -> PackState:
        return await self._installer.upgrade(source_path, allowed_root=allowed_root)

    async def enable(self, pack_id: str) -> PackState:
        return await self._installer.enable(pack_id)

    async def disable(self, pack_id: str) -> PackState:
        return await self._installer.disable(pack_id)

    async def uninstall(self, pack_id: str) -> bool:
        return await self._installer.uninstall(pack_id)

    def _remove_installed_path(
        self,
        install_path: str | Path,
        *,
        pack_id: str | None = None,
    ) -> None:
        """Remove one managed payload after lifecycle state is safe."""
        target = Path(install_path).resolve()
        if not _inside(self._root, target) or target == self._root:
            raise ValueError("pack install path is outside the managed pack root")
        if target.exists():
            if target.is_symlink() or not target.is_dir():
                raise ValueError("pack install path is not a managed directory")
            shutil.rmtree(target)
        parent = target.parent
        expected_parent = self._root / pack_id if pack_id else None
        if (
            expected_parent is not None
            and parent != expected_parent
            and parent.exists()
            and not any(parent.iterdir())
        ):
            parent.rmdir()

    @property
    def _integrations_bound(self) -> bool:
        return any(
            (
                self._skill_lifecycle is not None,
                self._workflow_store is not None,
                self._fabric is not None,
                self._mcp_adapter is not None,
                self._event_store is not None and self._task_intake is not None,
            )
        )

    async def _activate(self, state: PackState) -> None:
        """Make validated declarative contributions visible and callable."""
        await self._activator.activate(state)

    async def _deactivate(self, state: PackState, *, remove: bool) -> None:
        await self._activator.deactivate(state, remove=remove)

    async def _activate_hooks(self, state: PackState) -> list[tuple[str, str]]:
        """Register static event -> durable-task subscriptions from a pack."""
        from athena.packs.hook_activation import activate_pack_hooks

        return await activate_pack_hooks(self, state)

    async def _activate_skills(self, state: PackState) -> list[tuple[str, str]]:
        from athena.packs.contribution_activation import activate_pack_skills

        return await activate_pack_skills(self, state)

    async def _activate_mcp_servers(self, state: PackState) -> list[tuple[str, str]]:
        from athena.packs.contribution_activation import activate_pack_mcp_servers

        return await activate_pack_mcp_servers(self, state)

    async def _activate_workflows(self, state: PackState) -> list[tuple[str, str]]:
        from athena.packs.contribution_activation import activate_pack_workflows

        return await activate_pack_workflows(self, state)

    async def _activate_instruments(self, state: PackState) -> list[tuple[str, str]]:
        from athena.packs.contribution_activation import activate_pack_instruments

        return await activate_pack_instruments(self, state)

    async def _activate_capabilities(self, state: PackState) -> list[tuple[str, str]]:
        from athena.packs.contribution_activation import activate_pack_capabilities

        return await activate_pack_capabilities(self, state)

    async def _contributions(self, pack_id: str) -> list[dict[str, str]]:
        method = getattr(self._store, "contributions", None)
        return list(await method(pack_id)) if method is not None else []

    async def _save_contribution(
        self,
        pack_id: str,
        kind: str,
        contribution_id: str,
    ) -> None:
        method = getattr(self._store, "save_contribution", None)
        if method is not None:
            await method(pack_id, kind, contribution_id)

    async def _delete_contributions(self, pack_id: str) -> None:
        method = getattr(self._store, "delete_contributions", None)
        if method is not None:
            await method(pack_id)

    def health(self, state: PackState) -> dict[str, Any]:
        path = Path(state.install_path)
        if not path.is_dir():
            return {
                "pack_id": state.id,
                "status": "missing",
                "reason": "install path missing",
                "hook_dispatch": self.hook_dispatch_health(),
            }
        try:
            _source, manifest, integrity = self._validated_source(str(path))
        except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            return {
                "pack_id": state.id,
                "status": "invalid",
                "reason": str(exc),
                "hook_dispatch": self.hook_dispatch_health(),
            }
        status = (
            "healthy"
            if integrity == state.source_integrity and manifest == state.manifest
            else "stale"
        )
        return {
            "pack_id": state.id,
            "version": state.manifest.version,
            "status": status,
            "enabled": state.enabled,
            "integrity": integrity,
            "expected_integrity": state.source_integrity,
            "hook_dispatch": self.hook_dispatch_health(),
        }

    async def list(self) -> list[dict[str, Any]]:
        return [state.to_record() for state in await self._store.list()]

    async def inspect_installed(self, pack_id: str) -> dict[str, Any]:
        state = await self._store.get(pack_id)
        if state is None:
            raise KeyError(f"pack not found: {pack_id}")
        return {**state.to_record(), "health_detail": self.health(state)}

    def _mcp_client_factory(self, connection_id: str, **kwargs: Any) -> MCPClient:
        """Indirection preserves the manager-owned client seam for compatibility."""
        return MCPClient(connection_id, **kwargs)

    def _validated_source(self, source_path: str, *, allowed_root: str | None = None):
        source = Path(source_path).expanduser().resolve()
        if allowed_root is not None and not _inside(
            Path(allowed_root).expanduser().resolve(), source
        ):
            raise ValueError("pack source must be inside the task workspace")
        if not source.is_dir():
            raise ValueError("pack source must be a directory")
        if any(path.is_symlink() for path in source.rglob("*")):
            raise ValueError("pack source may not contain symbolic links")
        manifest_path = source / "athena.pack.toml"
        if not manifest_path.is_file():
            raise ValueError("pack is missing athena.pack.toml")
        with manifest_path.open("rb") as handle:
            raw = tomllib.load(handle)
        manifest = _parse_manifest(raw)
        _validate_provided_files(source, manifest)
        integrity = _directory_integrity(source)
        if manifest.declared_integrity and manifest.declared_integrity != integrity:
            raise ValueError("pack integrity hash does not match athena.pack.toml")
        return source, manifest, integrity


__all__ = ["PackManager"]
