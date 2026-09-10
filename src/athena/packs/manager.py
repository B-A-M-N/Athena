"""Validation and lifecycle for local declarative packs."""

from __future__ import annotations

import hashlib
import asyncio
import inspect
from importlib import import_module
import json
import logging
import os
import re
import shutil
import tempfile
import tarfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from athena.packs.models import PackManifest, PackState
from athena.mcp.client import MCPClient
from athena.network import (
    classify_endpoint,
    pinned_sync_transport,
    validate_endpoint,
    validate_target,
)
from athena.protocol.ids import stable_id
from athena.protocol.messages import utcnow
from athena.protocol.tasks import CapabilityPolicy, TrustedTaskMetadata

try:
    tomllib = import_module("tomllib")
except ModuleNotFoundError:  # pragma: no cover - legacy/minimal Python builds
    tomllib = import_module("tomli")


_PACK_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,2}(?:[-+][A-Za-z0-9.-]+)?$")
_EFFECTS = frozenset(
    {
        "READ_LOCAL",
        "WRITE_LOCAL",
        "EXECUTE",
        "SPAWN_PROCESS",
        "NETWORK_READ",
        "NETWORK_WRITE",
        "SECRET_READ",
        "DELETE",
        "PRIVILEGED",
        "EXTERNAL_MESSAGE",
        "EXTERNAL_PUBLISH",
        "COMPUTER_INPUT",
        "FINANCIAL",
    }
)


def _decode_pack_hook_causal(value: Any) -> Mapping[str, Any] | None:
    """Decode only the durable task-manager causal envelope."""
    raw = str(value or "")
    prefix = "athena-pack-hook:"
    if not raw.startswith(prefix):
        return None
    try:
        decoded = json.loads(raw[len(prefix) :])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, Mapping) else None


_PROVIDED_FILES = {
    "skills": (".md",),
    "workflows": (".json",),
    "capabilities": (".json",),
    "mcp_servers": (".json", ".toml"),
    "instruments": (".json",),
    "hooks": (".json", ".toml"),
}
_REMOTE_CONTENT_CACHE_LIMIT = 512 * 1024 * 1024
_REMOTE_CONTENT_CACHE_TTL = 7 * 24 * 60 * 60

_logger = logging.getLogger("athena.packs")


def _canonical_digest(value: Mapping[str, Any] | dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(value), sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


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
        self._hook_callbacks: dict[str, list[tuple[str, Any]]] = {}
        self._hook_contracts: dict[str, dict[str, Any]] = {}
        self._hook_events_seen: set[str] = set()
        self._hook_outbox = None
        self._hook_retry_task: asyncio.Task | None = None
        self._hook_health: dict[str, Any] = {
            "state": "stopped",
            "last_success_at": None,
            "last_error_at": None,
            "last_error": None,
            "iterations": 0,
        }

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
        self._task_intake = task_intake
        self._task_lookup = task_lookup
        self._workspace = workspace

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
        return dict(self._hook_health)

    async def replay_hook_outbox(self) -> int:
        """Replay hook deliveries that were durable before a process restart."""
        if self._hook_outbox is None:
            return 0
        callbacks = {
            hook_id: callback
            for values in self._hook_callbacks.values()
            for hook_id, callback in values
        }
        replayed = 0
        for row in await self._hook_outbox.pending():
            callback = callbacks.get(str(row.get("hook_id") or ""))
            if callback is None:
                continue
            claimed = await self._hook_outbox.claim(str(row.get("id") or ""))
            if claimed is None:
                continue
            row = claimed
            try:
                contract = self._hook_contracts.get(str(row.get("hook_id") or ""))
                if contract is None or row.get("hook_contract_digest") != contract["digest"]:
                    mark_stale = getattr(self._hook_outbox, "mark_stale_contract", None)
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
                await self._hook_outbox.mark_failed(str(row.get("id") or ""), str(exc), **kwargs)
            else:
                replayed += 1
        return replayed

    async def start_hook_dispatcher(self, interval_s: float = 1.0) -> None:
        """Keep durable hook retries moving after startup replay."""
        if self._hook_retry_task is not None or self._hook_outbox is None:
            return

        async def _loop() -> None:
            self._hook_health["state"] = "running"
            while True:
                try:
                    self._hook_health["iterations"] = (
                        int(self._hook_health.get("iterations", 0)) + 1
                    )
                    await self.replay_hook_outbox()
                    self._hook_health["last_success_at"] = utcnow().isoformat()
                    self._hook_health["last_error"] = None
                    await asyncio.sleep(max(0.1, float(interval_s)))
                except asyncio.CancelledError:
                    self._hook_health["state"] = "stopped"
                    raise
                except Exception as exc:  # noqa: BLE001 - one retry iteration must not stop the dispatcher
                    self._hook_health.update(
                        state="degraded",
                        last_error_at=utcnow().isoformat(),
                        last_error=str(exc)[:2000],
                    )
                    _logger.warning("pack hook retry iteration failed: %s", exc)
                    await asyncio.sleep(min(30.0, max(0.25, float(interval_s))))

        self._hook_retry_task = asyncio.create_task(_loop())

    async def stop_hook_dispatcher(self) -> None:
        task = self._hook_retry_task
        self._hook_retry_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _resolve_hook_workflow(self, state: PackState, workflow_id: str) -> str:
        if self._workflow_store is None:
            # A manager can be used in isolation by import/activation tools.
            # The service-bound path always supplies the store and performs
            # the provenance check below; preserve the declarative reference
            # for those deliberately unbound callers.
            return workflow_id
        for workflow in await self._workflow_store.list():
            provenance = dict(workflow.provenance or {})
            if provenance.get("pack_id") != state.id:
                continue
            if workflow.id == workflow_id or provenance.get("source_id") == workflow_id:
                return workflow.id
        raise ValueError(
            f"pack hook workflow {workflow_id!r} is not an active workflow from pack {state.id!r}"
        )

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
        max_bytes: int = 32 * 1024 * 1024,
        network_policy: str | object | None = None,
    ) -> dict[str, Any]:
        """Fetch one archive into a content-addressed quarantine directory.

        Remote bytes are never activated directly. Archives must be zip or
        tar-based, may not contain links or path escapes, and are validated as
        a normal declarative pack before the quarantine path is returned.
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
            return {
                "source_path": str(source),
                "archive_sha256": archive_hash,
                "pack_integrity": integrity,
                "manifest": manifest.to_record(computed_integrity=integrity),
                "quarantined": True,
                "operator_approval_required": True,
                "authenticity": {
                    "transport": endpoint.scheme,
                    "endpoint_classification": endpoint.classification,
                    "archive_sha256": archive_hash,
                    "operator_expected_sha256": expected or None,
                    "operator_approved": False,
                },
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
        approved: bool = False,
        enable: bool = True,
        network_policy: str | object | None = None,
    ) -> PackState:
        if not approved:
            raise PermissionError("remote pack installation requires explicit operator approval")
        fetched = self.fetch_remote(
            source_url,
            expected_sha256=expected_sha256,
            network_policy=network_policy,
        )
        return await self.install(fetched["source_path"], enable=enable)

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
            dict(item)
            for item in records[:500]
            if isinstance(item, Mapping)
            and (not needle or needle in json.dumps(item, sort_keys=True).casefold())
        ]

    async def install(
        self, source_path: str, *, allowed_root: str | None = None, enable: bool = True
    ) -> PackState:
        source, manifest, integrity = self._validated_source(source_path, allowed_root=allowed_root)
        target = self._root / manifest.id / manifest.version
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing = await self._store.get(manifest.id)
            if existing and existing.source_integrity == integrity:
                return (
                    await self.enable(manifest.id) if enable and not existing.enabled else existing
                )
            raise ValueError(f"pack version already installed: {manifest.id}@{manifest.version}")
        temp = Path(tempfile.mkdtemp(prefix=".pack-", dir=str(target.parent)))
        try:
            shutil.copytree(source, temp / "payload", symlinks=False)
            os.replace(temp / "payload", target)
        finally:
            shutil.rmtree(temp, ignore_errors=True)
        state = PackState(
            manifest=manifest,
            install_path=str(target),
            enabled=enable,
            installed_at=datetime.now(timezone.utc).isoformat(),
            source_integrity=integrity,
            health="healthy",
        )
        await self._store.save(state)
        if enable and self._integrations_bound:
            try:
                await self._activate(state)
            except Exception:  # noqa: BLE001 - disable durable state after admission failure
                # Do not leave a durable enabled bit for a pack whose live
                # contributions failed admission.
                await self._store.set_enabled(manifest.id, False)
                raise
        return state

    async def upgrade(self, source_path: str, *, allowed_root: str | None = None) -> PackState:
        _source, manifest, _integrity = self._validated_source(
            source_path, allowed_root=allowed_root
        )
        prior = await self._store.get(manifest.id)
        if prior is not None and self._integrations_bound:
            hooks_suspended = False
            if self._hook_outbox is not None:
                await self._hook_outbox.suspend_pack(manifest.id, "pack upgrade in progress")
                hooks_suspended = True

            async def resume_hooks() -> None:
                nonlocal hooks_suspended
                if hooks_suspended and self._hook_outbox is not None:
                    await self._hook_outbox.resume_pack(manifest.id)
                    hooks_suspended = False

            # Install the new payload disabled first. The old live
            # contributions remain available until the new payload is safely
            # copied and persisted, then the old version is removed and the
            # new version is activated.
            try:
                state = await self.install(
                    source_path,
                    allowed_root=allowed_root,
                    enable=False,
                )
                await self._deactivate(prior, remove=True)
                enabled = await self._store.set_enabled(manifest.id, True)
                if enabled is None:
                    raise RuntimeError(f"upgraded pack disappeared: {manifest.id}")
                try:
                    await self._activate(enabled)
                except Exception:  # noqa: BLE001 - optional contribution may already be gone
                    await self._store.set_enabled(manifest.id, False)
                    # The old contribution rows were removed before activation,
                    # so restore the prior durable state and live surface when
                    # the replacement fails admission.
                    try:
                        await self._store.save(prior)
                        await self._activate(prior)
                    except Exception as restore_error:  # noqa: BLE001 - report restore failure
                        raise RuntimeError(
                            f"pack upgrade failed and prior version could not be "
                            f"restored: {restore_error}"
                        ) from restore_error
                    raise
                self._remove_installed_path(prior.install_path)
                return enabled
            finally:
                # Every exit after suspension, including a failed store
                # transition or an unrecoverable activation failure, must
                # release outstanding hook obligations.
                await resume_hooks()
        state = await self.install(source_path, allowed_root=allowed_root, enable=True)
        if prior is not None and prior.manifest.version != state.manifest.version:
            await self._store.set_enabled(prior.id, False)
            self._remove_installed_path(prior.install_path)
        return state

    async def enable(self, pack_id: str) -> PackState:
        state = await self._store.set_enabled(pack_id, True)
        if state is None:
            raise KeyError(f"pack not found: {pack_id}")
        health = self.health(state)
        if health["status"] != "healthy":
            await self._store.set_enabled(pack_id, False)
            raise ValueError(f"pack integrity check failed: {pack_id}")
        if self._integrations_bound:
            try:
                await self._activate(state)
            except Exception:  # noqa: BLE001 - disable durable state after reactivation failure
                await self._store.set_enabled(pack_id, False)
                raise
        if self._hook_outbox is not None:
            await self._hook_outbox.resume_pack(pack_id)
        return state

    async def disable(self, pack_id: str) -> PackState:
        state = await self._store.set_enabled(pack_id, False)
        if state is None:
            raise KeyError(f"pack not found: {pack_id}")
        if self._integrations_bound:
            await self._deactivate(state, remove=False)
        if self._hook_outbox is not None:
            await self._hook_outbox.suspend_pack(pack_id)
        return state

    async def uninstall(self, pack_id: str) -> bool:
        state = await self._store.get(pack_id)
        if state is None:
            return False
        if self._integrations_bound:
            await self._deactivate(state, remove=True)
        if self._hook_outbox is not None:
            await self._hook_outbox.cancel_pack(pack_id)
        target = Path(state.install_path).resolve()
        self._remove_installed_path(target, pack_id=pack_id)
        return await self._store.delete(pack_id)

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
        existing = await self._contributions(state.id)
        if existing:
            await self._reactivate_existing(state, existing)
            return
        contributions: list[tuple[str, str]] = []
        try:
            for activator in (
                self._activate_skills,
                self._activate_workflows,
                self._activate_capabilities,
                self._activate_instruments,
                self._activate_mcp_servers,
                self._activate_hooks,
            ):
                created = await activator(state)
                contributions.extend(created)
                for kind, contribution_id in created:
                    await self._save_contribution(state.id, kind, contribution_id)
        except Exception:  # noqa: BLE001 - deactivate partial contributions during rollback
            await self._deactivate(state, remove=True)
            raise

    async def _reactivate_existing(
        self,
        state: PackState,
        contributions: list[dict[str, str]],
    ) -> None:
        """Restore live registrations after disable or a service restart."""
        for item in contributions:
            kind = item["kind"]
            contribution_id = item["contribution_id"]
            if kind == "workflow" and self._workflow_store is not None:
                workflow = await self._workflow_store.get(contribution_id)
                if workflow is not None and (
                    not workflow.enabled or workflow.lifecycle_state != "ACTIVE"
                ):
                    await self._workflow_store.save(
                        replace(workflow, enabled=True, lifecycle_state="ACTIVE")
                    )
            elif kind == "skill" and self._skill_lifecycle is not None:
                await self._skill_lifecycle.enable(contribution_id)
        if any(item["kind"] == "capability" for item in contributions):
            await self._activate_capabilities(state)
        if any(item["kind"] == "instrument" for item in contributions):
            await self._activate_instruments(state)
        if any(item["kind"] == "mcp" for item in contributions):
            await self._activate_mcp_servers(state)
        if any(item["kind"] == "hook" for item in contributions):
            await self._activate_hooks(state)

    async def _deactivate(self, state: PackState, *, remove: bool) -> None:
        for item in await self._contributions(state.id):
            kind = item["kind"]
            contribution_id = item["contribution_id"]
            if kind == "workflow" and self._workflow_store is not None:
                if remove:
                    await self._workflow_store.delete(contribution_id)
                else:
                    workflow = await self._workflow_store.get(contribution_id)
                    if workflow is not None:
                        await self._workflow_store.save(
                            replace(workflow, enabled=False, lifecycle_state="DISABLED")
                        )
            elif kind == "skill" and self._skill_lifecycle is not None:
                if remove:
                    await self._skill_lifecycle.archive(contribution_id)
                else:
                    await self._skill_lifecycle.disable(contribution_id)
            elif kind == "capability" and self._fabric is not None:
                registry = self._fabric.global_registry
                try:
                    descriptor = registry.resolve(contribution_id)
                except Exception:  # noqa: BLE001 - optional contribution may already be gone
                    descriptor = None
                if descriptor is not None and descriptor.origin.value == "plugin":
                    registry.unregister(contribution_id)
            elif kind == "instrument" and self._fabric is not None:
                registry = self._fabric.global_registry
                try:
                    descriptor = registry.resolve(contribution_id)
                except Exception:  # noqa: BLE001 - optional contribution may already be gone
                    descriptor = None
                if descriptor is not None and descriptor.origin.value == "plugin":
                    registry.unregister(contribution_id)
            elif kind == "mcp" and self._mcp_adapter is not None:
                self._mcp_adapter.unregister_connection(contribution_id)
                client = self._mcp_clients.pop(contribution_id, None)
                if client is not None:
                    await client.close()
            elif kind == "hook":
                for hook_id, callback in self._hook_callbacks.get(state.id, ()):
                    if hook_id == contribution_id and self._event_store is not None:
                        self._event_store.unsubscribe(callback)
                        self._hook_contracts.pop(hook_id, None)
        if remove:
            await self._delete_contributions(state.id)
            self._hook_callbacks.pop(state.id, None)

    async def _activate_hooks(self, state: PackState) -> list[tuple[str, str]]:
        """Register static event -> durable-task subscriptions from a pack."""
        if self._event_store is None or self._task_intake is None:
            return []
        created: list[tuple[str, str]] = []
        callbacks: list[tuple[str, Any]] = []
        for relative in state.manifest.provides.get("hooks", ()):
            path = Path(state.install_path) / relative
            raw = (
                tomllib.loads(path.read_text(encoding="utf-8"))
                if path.suffix.casefold() == ".toml"
                else json.loads(path.read_text(encoding="utf-8"))
            )
            records = raw.get("hooks", raw) if isinstance(raw, Mapping) else raw
            records = records if isinstance(records, list) else [records]
            for index, record in enumerate(records, 1):
                if not isinstance(record, Mapping):
                    raise ValueError(f"pack hook must be an object: {relative}")
                event_type = str(record.get("event") or record.get("event_type") or "").strip()
                workflow_id = str(record.get("workflow") or "").strip()
                if (
                    not event_type
                    or len(event_type) > 128
                    or not workflow_id
                    or len(workflow_id) > 256
                ):
                    raise ValueError("pack hooks require bounded event and workflow fields")
                workflow_id = await self._resolve_hook_workflow(state, workflow_id)
                workflow_integrity = None
                if self._workflow_store is not None:
                    workflow = await self._workflow_store.get(workflow_id)
                    if workflow is None:
                        raise ValueError(f"pack hook workflow {workflow_id!r} is unavailable")
                    workflow_integrity = _canonical_digest(workflow.to_record())
                raw_effects = record.get("effects") or record.get("requested_effects") or ()
                if not isinstance(raw_effects, (list, tuple, set, frozenset)):
                    raise ValueError("pack hook effects must be an array")
                effect_ceiling = tuple(sorted(str(item) for item in raw_effects))
                if any(item not in state.manifest.requested_effects for item in effect_ceiling):
                    raise ValueError("pack hook effects exceed the pack authority ceiling")
                try:
                    recursion_limit = int(record.get("recursion_limit", 3))
                except (TypeError, ValueError) as exc:
                    raise ValueError("pack hook recursion_limit must be an integer") from exc
                if not 0 <= recursion_limit <= 3:
                    raise ValueError("pack hook recursion_limit must be between 0 and 3")
                hook_id = f"pack:{state.id}:hook:{index}"
                contract = {
                    "pack_id": state.id,
                    "pack_version": state.manifest.version,
                    "pack_integrity": state.source_integrity,
                    "workflow_id": workflow_id,
                    "workflow_integrity": workflow_integrity,
                    "effect_ceiling": list(effect_ceiling),
                    "recursion_limit": recursion_limit,
                }
                contract["digest"] = _canonical_digest(contract)
                self._hook_contracts[hook_id] = contract

                async def on_event(
                    event,
                    *,
                    _event_type=event_type,
                    _workflow=workflow_id,
                    _hook_id=hook_id,
                    _effect_ceiling=effect_ceiling,
                    _recursion_limit=recursion_limit,
                    _contract=contract,
                ):
                    event_id = str(getattr(event, "id", "") or "")
                    if not event_id:
                        return
                    raw_event_payload = getattr(event, "payload", {}) or {}
                    event_payload = (
                        dict(raw_event_payload) if isinstance(raw_event_payload, Mapping) else {}
                    )
                    causal = _decode_pack_hook_causal(getattr(event, "causal_id", None))
                    if not isinstance(causal, Mapping):
                        causal = {}
                    try:
                        depth = int(causal.get("depth", 0))
                    except (TypeError, ValueError):
                        depth = _recursion_limit + 1
                    if causal and causal.get("kind") != "pack_hook":
                        return
                    if depth > _recursion_limit:
                        return
                    outbox_row = getattr(event, "_hook_outbox_row", None)
                    if self._hook_outbox is not None:
                        if outbox_row is None:
                            outbox_row = await self._hook_outbox.enqueue(
                                pack_id=state.id,
                                hook_id=_hook_id,
                                event_id=event_id,
                                event_type=_event_type,
                                task_id=getattr(event, "task_id", None),
                                session_id=getattr(event, "session_id", None),
                                payload=event_payload,
                                depth=depth,
                                pack_version=_contract["pack_version"],
                                pack_integrity=_contract["pack_integrity"],
                                workflow_id=_contract["workflow_id"],
                                workflow_integrity=_contract["workflow_integrity"],
                                effect_ceiling=_contract["effect_ceiling"],
                                recursion_limit=_contract["recursion_limit"],
                                hook_contract_digest=_contract["digest"],
                            )
                            if str(outbox_row.get("status") or "") == "DISPATCHED":
                                return
                            claim = getattr(self._hook_outbox, "claim", None)
                            if claim is not None:
                                claimed = await claim(str(outbox_row.get("id") or ""))
                                if claimed is None:
                                    return
                                outbox_row = claimed
                    elif event_id in self._hook_events_seen:
                        return
                    else:
                        self._hook_events_seen.add(event_id)
                    from athena.protocol.tasks import AgentRequest

                    encoded_payload = json.dumps(event_payload, sort_keys=True, default=str)
                    if len(encoded_payload) > 8000:
                        invocation_payload: Mapping[str, Any] = {
                            "truncated": True,
                            "preview": encoded_payload[:7900],
                        }
                    else:
                        invocation_payload = (
                            json.loads(encoded_payload)
                            if encoded_payload.startswith("{")
                            else {"value": encoded_payload}
                        )
                    payload = encoded_payload[:8000]
                    hook_task_id = str(
                        outbox_row.get("hook_task_id")
                        if outbox_row is not None
                        else stable_id("pack-hook-task", _hook_id, event_id)
                    )
                    hook_session_id = str(
                        outbox_row.get("hook_session_id")
                        if outbox_row is not None and outbox_row.get("hook_session_id")
                        else stable_id("pack-hook-session", _hook_id, event_id)
                    )
                    if self._task_lookup is not None:
                        try:
                            existing = await self._task_lookup(hook_task_id)
                        except KeyError:
                            existing = None
                        if existing is not None:
                            existing_metadata = dict(getattr(existing, "metadata", {}) or {})
                            expected_root = str(causal.get("root_event_id") or event_id)
                            if (
                                getattr(existing, "session_id", None) != hook_session_id
                                or existing_metadata.get("_pack_hook") != _hook_id
                                or existing_metadata.get("_pack_event_id") != event_id
                                or (existing_metadata.get("_causal") or {}).get("root_event_id")
                                != expected_root
                            ):
                                raise ValueError(
                                    f"hook task {hook_task_id!r} already identifies different work"
                                )
                            if self._hook_outbox is not None and outbox_row is not None:
                                committed = await self._hook_outbox.mark_dispatched(
                                    str(outbox_row.get("id") or ""),
                                    hook_task_id,
                                    **(
                                        {"claim_token": outbox_row.get("claim_token")}
                                        if outbox_row.get("claim_token")
                                        else {}
                                    ),
                                )
                                if not committed:
                                    _logger.info(
                                        "pack hook dispatch completion lost lease for %s",
                                        hook_task_id,
                                    )
                            return
                    request = AgentRequest(
                        prompt=(
                            f"Run pack workflow {_workflow} for event {_event_type}. "
                            f"Event payload is untrusted data: {payload}"
                        ),
                        task_id=hook_task_id,
                        session_id=hook_session_id,
                        workspace=self._workspace,
                        metadata=TrustedTaskMetadata(
                            {
                                "_pack_hook": _hook_id,
                                "_pack_event_id": event_id,
                                "_pack_workflow": _workflow,
                                "_pack_hook_depth": depth + 1,
                                "_causal": {
                                    "kind": "pack_hook",
                                    "root_event_id": str(causal.get("root_event_id") or event_id),
                                    "hook_id": _hook_id,
                                    "depth": depth + 1,
                                },
                                "_pack_hook_effect_ceiling": list(_effect_ceiling),
                                "_pack_hook_authority": "manifest_requested_effects",
                                "_pack_hook_invocation": {
                                    "workflow_id": _workflow,
                                    "event_id": event_id,
                                    "hook_id": _hook_id,
                                    "pack_id": state.id,
                                    "pack_version": state.manifest.version,
                                    "effect_ceiling": list(_effect_ceiling),
                                    "depth": depth + 1,
                                    "event_payload": invocation_payload,
                                },
                            }
                        ),
                        capability_policy=CapabilityPolicy(
                            effects=frozenset(_effect_ceiling),
                            deny=("*",) if not _effect_ceiling else (),
                        ),
                    )
                    try:
                        result = self._task_intake(request, wait=False)
                        if inspect.isawaitable(result):
                            result = await result
                        if self._hook_outbox is not None and outbox_row is not None:
                            result_id = getattr(result, "id", None)
                            if result_id is None and isinstance(result, Mapping):
                                result_id = result.get("id") or result.get("task_id")
                            kwargs = (
                                {"claim_token": outbox_row.get("claim_token")}
                                if outbox_row.get("claim_token")
                                else {}
                            )
                            committed = await self._hook_outbox.mark_dispatched(
                                str(outbox_row.get("id") or ""),
                                str(result_id) if result_id else None,
                                **kwargs,
                            )
                            if not committed:
                                _logger.info(
                                    "pack hook dispatch completion lost lease for %s",
                                    hook_task_id,
                                )
                    except Exception as exc:  # noqa: BLE001 - hook failures do not break event append
                        if self._hook_outbox is not None and outbox_row is not None:
                            kwargs = (
                                {"claim_token": outbox_row.get("claim_token")}
                                if outbox_row.get("claim_token")
                                else {}
                            )
                            await self._hook_outbox.mark_failed(
                                str(outbox_row.get("id") or ""), str(exc), **kwargs
                            )
                        _logger.warning("pack hook %s could not enqueue: %s", _hook_id, exc)

                self._event_store.subscribe(on_event, event_types={event_type})
                callbacks.append((hook_id, on_event))
                created.append(("hook", hook_id))
        self._hook_callbacks[state.id] = callbacks
        return created

    async def _activate_skills(self, state: PackState) -> list[tuple[str, str]]:
        if self._skill_lifecycle is None:
            return []
        from athena.protocol.messages import TrustClass
        from athena.skills.loader import SkillLoader

        loader = SkillLoader()
        results: list[tuple[str, str]] = []
        try:
            for relative in state.manifest.provides.get("skills", ()):
                path = Path(state.install_path) / relative
                skill = loader.parse_skill_file(
                    path,
                    scope="user",
                    trust=TrustClass.CONFIGURED_INSTRUCTION,
                )
                if skill is None:
                    raise ValueError(f"pack skill is invalid: {relative}")
                skill_id = (
                    f"pack:{state.id}:skill:{hashlib.sha256(relative.encode()).hexdigest()[:16]}"
                )
                skill = replace(
                    skill,
                    id=skill_id,
                    metadata={
                        **dict(skill.metadata),
                        "pack_id": state.id,
                        "pack_version": state.manifest.version,
                    },
                )
                await self._skill_lifecycle.install(skill)
                results.append(("skill", skill_id))
        except Exception:  # noqa: BLE001 - roll back installed skills, then preserve failure
            for _, skill_id in results:
                try:
                    await self._skill_lifecycle.archive(skill_id)
                except Exception:  # noqa: BLE001 - rollback is best effort after failed activation
                    pass
            raise
        return results

    async def _activate_mcp_servers(self, state: PackState) -> list[tuple[str, str]]:
        """Connect declarative pack MCP servers through the existing adapter."""
        files = state.manifest.provides.get("mcp_servers", ())
        if not files:
            return []
        if self._mcp_adapter is None:
            raise RuntimeError("MCP pack contributions are not available")

        created: list[tuple[str, str]] = []
        try:
            for relative in files:
                path = Path(state.install_path) / relative
                if path.suffix.lower() == ".toml":
                    raw = tomllib.loads(path.read_text(encoding="utf-8"))
                else:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                records = raw.get("servers", raw) if isinstance(raw, Mapping) else raw
                records = records if isinstance(records, list) else [records]
                for index, record in enumerate(records, 1):
                    if not isinstance(record, Mapping):
                        raise ValueError(f"pack MCP server must be an object: {relative}")
                    name = str(record.get("name") or f"server-{index}")
                    command = record.get("command")
                    url = record.get("url")
                    if (command is None) == (url is None):
                        raise ValueError(
                            f"pack MCP server {name!r} requires exactly one command or url"
                        )
                    requested = set(state.manifest.requested_effects)
                    transport_effect = "SPAWN_PROCESS" if command is not None else "NETWORK_READ"
                    if transport_effect not in requested:
                        raise ValueError(
                            f"pack MCP server {name!r} requires manifest authority "
                            f"{transport_effect} for activation"
                        )
                    connection_id = f"pack:{state.id}:{name}"
                    client: MCPClient | None = None
                    try:
                        client = MCPClient(
                            connection_id,
                            command=str(command) if command is not None else None,
                            args=[str(item) for item in record.get("args") or ()],
                            url=str(url) if url is not None else None,
                            env={
                                str(key): str(value)
                                for key, value in (record.get("env") or {}).items()
                            },
                            connect_timeout=float(record.get("connect_timeout", 10.0)),
                        )
                        await client.connect()
                        descriptors = await self._mcp_adapter.collect_and_register(
                            client,
                            server_alias=connection_id,
                        )
                        effects = {
                            effect.value
                            for descriptor in descriptors
                            for effect in descriptor.effects
                        }
                        if not effects.issubset(requested):
                            raise ValueError(
                                f"pack MCP server {name!r} exceeds requested effects: "
                                + ", ".join(sorted(effects - requested))
                            )
                        self._mcp_clients[connection_id] = client
                        if self._mcp_client_sink is not None:
                            self._mcp_client_sink(client)
                        created.append(("mcp", connection_id))
                    except Exception:  # noqa: BLE001 - clean up failed MCP admission
                        self._mcp_adapter.unregister_connection(connection_id)
                        if client is not None:
                            await client.close()
                        raise
        except Exception:  # noqa: BLE001 - roll back installed workflows, then preserve failure
            for _, connection_id in created:
                if self._mcp_adapter is not None:
                    self._mcp_adapter.unregister_connection(connection_id)
                client = self._mcp_clients.pop(connection_id, None)
                if client is not None:
                    await client.close()
            raise
        return created

    async def _activate_workflows(self, state: PackState) -> list[tuple[str, str]]:
        if self._workflow_store is None:
            return []
        from athena.affordances.models import AffordanceScope
        from athena.workflows.models import Workflow, WorkflowStep
        from athena.workflows.validation import WorkflowValidator

        raw_workflows: list[dict[str, Any]] = []
        for relative in state.manifest.provides.get("workflows", ()):
            value = json.loads((Path(state.install_path) / relative).read_text(encoding="utf-8"))
            records = value if isinstance(value, list) else [value]
            if not all(isinstance(item, Mapping) for item in records):
                raise ValueError(f"pack workflow file must contain objects: {relative}")
            raw_workflows.extend(dict(item) for item in records)
        ids = {
            str(record.get("id") or f"workflow_{index}"): f"pack:{state.id}:workflow:{index}"
            for index, record in enumerate(raw_workflows, 1)
        }
        workflows: list[Workflow] = []
        for index, record in enumerate(raw_workflows, 1):
            original_id = str(record.get("id") or f"workflow_{index}")
            steps: list[WorkflowStep] = []
            for step_record in record.get("steps") or ():
                step = WorkflowStep.from_record(step_record, len(steps))
                if step.workflow_id:
                    step = replace(step, workflow_id=ids.get(step.workflow_id, step.workflow_id))
                steps.append(step)
            workflow = Workflow.from_record(
                {
                    **record,
                    "id": ids[original_id],
                    "steps": [step.to_record() for step in steps],
                    "scope": AffordanceScope.SYSTEM.value,
                    "task_scope": None,
                    "project_scope": None,
                    "user_scope": None,
                    "provenance": {
                        **dict(record.get("provenance") or {}),
                        "pack_id": state.id,
                        "pack_version": state.manifest.version,
                        "source_id": original_id,
                    },
                }
            )
            workflows.append(workflow)
        workflow_by_id = {workflow.id: workflow for workflow in workflows}

        def resolver(identifier: str):
            if identifier in workflow_by_id:
                return workflow_by_id[identifier]
            if self._fabric is None:
                raise ValueError(f"pack workflow dependency unavailable: {identifier}")
            return self._fabric.global_registry.resolve(identifier)

        saved: list[str] = []
        try:
            for workflow in workflows:
                validation = WorkflowValidator(resolver).validate(workflow)
                if not validation.ok:
                    raise ValueError(
                        f"pack workflow {workflow.id} is invalid: {'; '.join(validation.errors)}"
                    )
                requested = set(state.manifest.requested_effects)
                workflow_effects = set(validation.effects)
                if not workflow_effects.issubset(requested):
                    raise ValueError(
                        f"pack workflow {workflow.id} exceeds requested effects: "
                        + ", ".join(sorted(workflow_effects - requested))
                    )
                await self._workflow_store.save(workflow)
                saved.append(workflow.id)
        except Exception:  # noqa: BLE001 - roll back saved workflows
            for workflow_id in saved:
                try:
                    await self._workflow_store.delete(workflow_id)
                except Exception:  # noqa: BLE001 - rollback is best effort after failed activation
                    pass
            raise
        return [("workflow", workflow.id) for workflow in workflows]

    async def _activate_instruments(self, state: PackState) -> list[tuple[str, str]]:
        """Expose pack-declared instrument views through governed aliases.

        An instrument contribution is intentionally a capability alias rather
        than an out-of-band UI callback.  Invoking it still enters the normal
        dispatcher and the result carries a bounded ``InstrumentView`` record.
        Pack data can therefore add a useful surface without loading code or
        bypassing policy.
        """
        if self._fabric is None or self._dispatcher is None:
            return []
        from athena.protocol.instruments import InstrumentView

        registry = self._fabric.global_registry
        created: list[str] = []
        try:
            for relative in state.manifest.provides.get("instruments", ()):
                value = json.loads(
                    (Path(state.install_path) / relative).read_text(encoding="utf-8")
                )
                records = value if isinstance(value, list) else [value]
                for index, record in enumerate(records, 1):
                    if not isinstance(record, Mapping):
                        raise ValueError(f"pack instrument must be an object: {relative}")
                    target_id = str(record.get("target") or record.get("capability") or "")
                    if not target_id:
                        raise ValueError(f"pack instrument requires target capability: {relative}")
                    target = registry.executor_for(target_id)
                    requested = set(state.manifest.requested_effects)
                    target_effects = {effect.value for effect in target.descriptor.effects}
                    if not target_effects.issubset(requested):
                        raise ValueError(
                            f"pack instrument target {target_id!r} exceeds requested effects"
                        )
                    raw_view = record.get("view") or record.get("instrument")
                    if not isinstance(raw_view, Mapping):
                        raise ValueError(f"pack instrument requires a view object: {relative}")
                    view = InstrumentView.from_record(raw_view)
                    alias_id = str(record.get("id") or (f"pack:{state.id}:instrument:{index}"))
                    if _registered(registry, alias_id):
                        raise ValueError(f"pack instrument already registered: {alias_id}")
                    alias = _InstrumentAlias(
                        alias_id=alias_id,
                        target=target,
                        dispatcher=self._dispatcher,
                        defaults=dict(record.get("defaults") or {}),
                        input_schema=dict(
                            record.get("input_schema") or target.descriptor.input_schema
                        ),
                        view=view,
                    )
                    registry.register(alias, authority=f"pack:{state.id}")
                    created.append(alias_id)
        except Exception:  # noqa: BLE001 - roll back registered instruments
            for alias_id in created:
                registry.unregister(alias_id)
            raise
        return [("instrument", alias_id) for alias_id in created]

    async def _activate_capabilities(self, state: PackState) -> list[tuple[str, str]]:
        """Activate only declarative aliases to existing capabilities."""
        if self._fabric is None or self._dispatcher is None:
            return []
        registry = self._fabric.global_registry
        results: list[tuple[str, str]] = []
        try:
            for relative in state.manifest.provides.get("capabilities", ()):
                value = json.loads(
                    (Path(state.install_path) / relative).read_text(encoding="utf-8")
                )
                records = value if isinstance(value, list) else [value]
                for record in records:
                    if not isinstance(record, Mapping):
                        raise ValueError(f"pack capability must be an object: {relative}")
                    alias_id = str(record.get("id") or "")
                    target_id = str(record.get("target") or "")
                    if not alias_id or not target_id or alias_id == target_id:
                        raise ValueError("pack capability alias requires distinct id and target")
                    target = registry.executor_for(target_id)
                    requested = set(state.manifest.requested_effects)
                    target_effects = {effect.value for effect in target.descriptor.effects}
                    if not target_effects.issubset(requested):
                        raise ValueError(
                            f"pack alias {alias_id} requests less authority than target {target_id}"
                        )
                    alias = _DeclarativeAlias(
                        alias_id=alias_id,
                        description=str(record.get("description") or target.descriptor.description),
                        target=target,
                        dispatcher=self._dispatcher,
                        defaults=dict(record.get("defaults") or {}),
                        input_schema=dict(
                            record.get("input_schema") or target.descriptor.input_schema
                        ),
                    )
                    if _registered(registry, alias_id):
                        continue
                    registry.register(alias, authority=f"pack:{state.id}")
                    results.append(("capability", alias_id))
        except Exception:  # noqa: BLE001 - roll back registered capability aliases
            for _, alias_id in results:
                registry.unregister(alias_id)
            raise
        return results

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


def _parse_manifest(raw: Mapping[str, Any]) -> PackManifest:
    pack_id = str(raw.get("id") or "")
    version = str(raw.get("version") or "")
    if not _PACK_ID.fullmatch(pack_id):
        raise ValueError("pack id must be lowercase and contain only letters, digits, _, ., -")
    if not _VERSION.fullmatch(version):
        raise ValueError("pack version must be numeric semver-like text")
    provides_raw = raw.get("provides") or {}
    if not isinstance(provides_raw, Mapping):
        raise ValueError("pack provides must be a table")
    provides: dict[str, tuple[str, ...]] = {}
    for kind, _suffixes in _PROVIDED_FILES.items():
        value = provides_raw.get(kind) or ()
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"pack provides.{kind} must be an array")
        provides[kind] = tuple(str(item) for item in value)
    authority = raw.get("authority") or {}
    if not isinstance(authority, Mapping):
        raise ValueError("pack authority must be a table")
    requested = tuple(str(item) for item in authority.get("requested_effects") or ())
    unknown = set(requested) - _EFFECTS
    if unknown:
        raise ValueError("pack requests unknown effects: " + ", ".join(sorted(unknown)))
    integrity = raw.get("integrity") or {}
    declared = integrity.get("sha256")
    if declared is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", str(declared)):
        raise ValueError("pack integrity.sha256 must be a 64-character hex digest")
    return PackManifest(
        id=pack_id,
        version=version,
        publisher=str(raw.get("publisher") or ""),
        minimum_athena=(str(raw["minimum_athena"]) if raw.get("minimum_athena") else None),
        provides=provides,
        requested_effects=requested,
        declared_integrity=str(declared).lower() if declared else None,
        metadata=dict(raw.get("metadata") or {}),
    )


def _govern_remote_target(source_url: str, network_policy: str | object | None):
    """Apply the same outbound target gate used by other network surfaces."""
    target, error = validate_target(source_url, network_policy)
    if error:
        raise PermissionError(error)
    if target is None:  # defensive narrowing for custom validator seams
        raise PermissionError("network target validation failed")
    return target


def _download_remote(
    source_url: str,
    *,
    target,
    max_bytes: int,
    timeout: float,
    user_agent: str,
    destination: Path | None = None,
) -> bytes:
    """Read one bounded, non-redirecting pack response.

    Restricted targets use the addresses validated before the request, closing
    the DNS-rebinding window.  The allow-policy compatibility path retains
    urllib's small test seam but still rejects redirects.
    """
    if max_bytes <= 0:
        raise ValueError("remote response size limit must be positive")
    headers = {"User-Agent": user_agent}
    if target.addresses:
        import httpx

        transport = pinned_sync_transport(target.hostname, target.addresses)
        try:
            with httpx.Client(
                transport=transport,
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                headers=headers,
            ) as client:
                with client.stream("GET", source_url) as response:
                    if response.status_code >= 300:
                        raise ValueError(
                            f"remote pack fetch returned HTTP {response.status_code}; redirects are not followed"
                        )
                    return _bounded_response(
                        response.iter_bytes(), max_bytes, destination=destination
                    )
        finally:
            transport.close()

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(source_url, headers=headers)
    with opener.open(request, timeout=timeout) as response:
        if int(getattr(response, "status", 200)) >= 300:
            raise ValueError(
                f"remote pack fetch returned HTTP {getattr(response, 'status', 0)}; redirects are not followed"
            )
        return _bounded_response(
            iter(lambda: response.read(1024 * 1024), b""), max_bytes, destination=destination
        )


def _bounded_response(chunks, max_bytes: int, *, destination: Path | None = None) -> bytes:
    if destination is not None:
        size = 0
        with destination.open("wb") as output:
            for chunk in chunks:
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("remote pack response exceeds size limit")
                output.write(bytes(chunk))
        return b""
    values: list[bytes] = []
    size = 0
    for chunk in chunks:
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise ValueError("remote pack response exceeds size limit")
        values.append(bytes(chunk))
    return b"".join(values)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _extract_archive_safely(
    archive: Path,
    destination: Path,
    *,
    max_total_bytes: int = 256 * 1024 * 1024,
    max_member_bytes: int = 64 * 1024 * 1024,
    max_members: int = 10_000,
    max_path_depth: int = 32,
) -> None:
    """Extract a pack archive with traversal, link, and decompression quotas."""
    if max_total_bytes <= 0 or max_member_bytes <= 0 or max_members <= 0:
        raise ValueError("archive extraction limits must be positive")
    total_bytes = 0
    member_count = 0

    def admit(name: str, declared_size: int) -> Path:
        nonlocal total_bytes, member_count
        member_count += 1
        if member_count > max_members:
            raise ValueError("remote pack archive contains too many members")
        if len(Path(name).parts) > max_path_depth:
            raise ValueError("remote pack archive path is too deep")
        if declared_size < 0 or declared_size > max_member_bytes:
            raise ValueError("remote pack archive member exceeds size limit")
        total_bytes += declared_size
        if total_bytes > max_total_bytes:
            raise ValueError("remote pack archive exceeds uncompressed size limit")
        return _safe_archive_target(destination, name)

    def copy_bounded(source, target: Path, declared_size: int) -> None:
        written = 0
        with target.open("wb") as out:
            while True:
                chunk = source.read(min(1024 * 1024, max_member_bytes - written + 1))
                if not chunk:
                    break
                written += len(chunk)
                if written > max_member_bytes or written > declared_size:
                    raise ValueError("remote pack archive member expands beyond its declared size")
                out.write(chunk)
        if written != declared_size:
            raise ValueError("remote pack archive member size does not match its declaration")

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zip_handle:
            for info in zip_handle.infolist():
                name = str(info.filename).replace("\\", "/")
                target = admit(name, int(info.file_size))
                if name.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("remote pack archive may not contain links")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zip_handle.open(info) as source:
                    copy_bounded(source, target, int(info.file_size))
        return
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as tar_handle:
            for member in tar_handle.getmembers():
                if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                    raise ValueError("remote pack archive may contain only regular files")
                target = admit(member.name, 0 if member.isdir() else int(member.size))
                target.parent.mkdir(parents=True, exist_ok=True)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                extracted = tar_handle.extractfile(member)
                if extracted is None:
                    raise ValueError("remote pack archive contains an unreadable file")
                with extracted:
                    copy_bounded(extracted, target, int(member.size))
        return
    raise ValueError("remote pack must be a zip or tar archive")


def _safe_archive_target(destination: Path, name: str) -> Path:
    if not name or name.startswith("/"):
        raise ValueError("remote pack archive contains an absolute path")
    target = (destination / name).resolve()
    try:
        target.relative_to(destination.resolve())
    except ValueError as exc:
        raise ValueError("remote pack archive contains a path traversal") from exc
    return target


def _find_pack_root(extracted: Path) -> Path:
    candidates = [path.parent for path in extracted.rglob("athena.pack.toml")]
    if len(candidates) != 1:
        raise ValueError("remote pack archive must contain exactly one athena.pack.toml")
    root = candidates[0]
    if not root.is_dir():
        raise ValueError("remote pack manifest root is not a directory")
    return root


def _validated_source_for_remote(source: Path) -> None:
    if any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError("remote pack may not contain symbolic links")


def _validate_provided_files(source: Path, manifest: PackManifest) -> None:
    for kind, names in manifest.provides.items():
        suffixes = _PROVIDED_FILES[kind]
        for name in names:
            path = (source / name).resolve()
            if not _inside(source, path) or path == source or not path.is_file():
                raise ValueError(f"pack contribution is not a regular in-pack file: {name}")
            if path.suffix.lower() not in suffixes:
                raise ValueError(f"pack {kind} contribution has unsupported type: {name}")


def _directory_integrity(root: Path) -> str:
    digest = hashlib.sha256()
    # The manifest may contain the digest of the payload. Including the
    # manifest itself would make the declared hash self-referential.
    for path in sorted(
        item
        for item in root.rglob("*")
        if item.is_file() and item.relative_to(root).as_posix() != "athena.pack.toml"
    ):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _inside(root: Path, target: Path) -> bool:
    try:
        return os.path.commonpath((str(root), str(target))) == str(root)
    except ValueError:
        return False


def _registered(registry: Any, capability_id: str) -> bool:
    try:
        registry.resolve(capability_id)
    except Exception:  # noqa: BLE001 - registry lookup treats missing optional capability as false
        return False
    return True


class _DeclarativeAlias:
    """Data-only pack alias that re-enters the canonical dispatcher."""

    def __init__(
        self,
        *,
        alias_id,
        description,
        target,
        dispatcher,
        defaults,
        input_schema,
    ):
        from athena.protocol.capabilities import CapabilityDescriptor, CapabilityOrigin

        self._target = target
        self._dispatcher = dispatcher
        self._defaults = dict(defaults)
        self.descriptor = CapabilityDescriptor(
            id=alias_id,
            description=f"[pack alias] {description}",
            input_schema=input_schema,
            output_schema=target.descriptor.output_schema,
            effects=target.descriptor.effects,
            origin=CapabilityOrigin.PLUGIN,
            effect_resolver=lambda arguments: (
                target.descriptor.resolve_effects({**self._defaults, **dict(arguments)})
                or target.descriptor.effects
            ),
        )

    async def invoke(self, request, *, context=None, **kwargs):
        from athena.capabilities.dispatcher import SuspendedCall
        from athena.protocol.capabilities import CapabilityRequest, CapabilityResult
        from athena.protocol.capabilities import CapabilityResultStatus

        del kwargs
        if context is None:
            raise ValueError("pack alias requires invocation context")
        arguments = {**self._defaults, **dict(request.arguments or {})}
        result = await self._dispatcher.dispatch(
            CapabilityRequest(
                capability_id=self._target.descriptor.id,
                arguments=arguments,
                task_id=request.task_id,
                session_id=request.session_id,
                call_id=request.call_id,
                origin=request.origin,
            ),
            workspace=context.workspace,
            profile=getattr(context, "autonomy", None),
            task_policy=getattr(context, "capability_policy", None),
            task_budget=getattr(context, "resource_budget", None),
        )
        if isinstance(result, SuspendedCall):
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                error="pack alias target requires approval and cannot suspend an alias call",
            )
        return replace(
            result,
            call_id=request.call_id,
            capability_id=request.capability_id,
        )


class _InstrumentAlias(_DeclarativeAlias):
    """Declarative target alias that contributes a bounded presentation view."""

    def __init__(self, *, alias_id, target, dispatcher, defaults, input_schema, view):
        super().__init__(
            alias_id=alias_id,
            description=f"instrument for {target.descriptor.id}",
            target=target,
            dispatcher=dispatcher,
            defaults=defaults,
            input_schema=input_schema,
        )
        self._view = view

    async def invoke(self, request, *, context=None, **kwargs):
        result = await super().invoke(request, context=context, **kwargs)
        if result.status.value != "ok":
            return result
        metadata = dict(result.metadata or {})
        metadata["instrument"] = self._view.to_record()
        return replace(result, metadata=metadata)


__all__ = ["PackManager"]
