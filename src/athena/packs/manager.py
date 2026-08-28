"""Validation and lifecycle for local declarative packs."""

from __future__ import annotations

import hashlib
from importlib import import_module
import json
import os
import re
import shutil
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from athena.packs.models import PackManifest, PackState
from athena.mcp.client import MCPClient

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
_PROVIDED_FILES = {
    "skills": (".md",),
    "workflows": (".json",),
    "capabilities": (".json",),
    "mcp_servers": (".json", ".toml"),
    "instruments": (".json",),
}


class PackManager:
    """Manage packs without loading pack code into Athena's interpreter."""

    def __init__(self, store, *, install_root: str) -> None:
        self._store = store
        self._root = Path(install_root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._skill_lifecycle = None
        self._workflow_store = None
        self._fabric = None
        self._dispatcher = None
        self._mcp_adapter = None
        self._mcp_client_sink = None
        self._mcp_clients: dict[str, MCPClient] = {}

    def bind_integrations(
        self,
        *,
        skill_lifecycle=None,
        workflow_store=None,
        fabric=None,
        dispatcher=None,
        mcp_adapter=None,
        mcp_client_sink=None,
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

    async def rehydrate_enabled(self) -> int:
        """Activate enabled packs after the host rebuilds its surfaces."""
        if not self._integrations_bound:
            return 0
        activated = 0
        for state in await self._store.list():
            if not state.enabled:
                continue
            await self._activate(state)
            activated += 1
        return activated

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
            except Exception:
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
            # Install the new payload disabled first. The old live
            # contributions remain available until the new payload is safely
            # copied and persisted, then the old version is removed and the
            # new version is activated.
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
            except Exception:
                await self._store.set_enabled(manifest.id, False)
                # The old contribution rows were removed before activation,
                # so restore the prior durable state and live surface when
                # the replacement fails admission.
                try:
                    await self._store.save(prior)
                    await self._activate(prior)
                except Exception as restore_error:
                    raise RuntimeError(
                        f"pack upgrade failed and prior version could not be "
                        f"restored: {restore_error}"
                    )
                raise
            self._remove_installed_path(prior.install_path)
            return enabled
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
            except Exception:
                await self._store.set_enabled(pack_id, False)
                raise
        return state

    async def disable(self, pack_id: str) -> PackState:
        state = await self._store.set_enabled(pack_id, False)
        if state is None:
            raise KeyError(f"pack not found: {pack_id}")
        if self._integrations_bound:
            await self._deactivate(state, remove=False)
        return state

    async def uninstall(self, pack_id: str) -> bool:
        state = await self._store.get(pack_id)
        if state is None:
            return False
        if self._integrations_bound:
            await self._deactivate(state, remove=True)
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
            ):
                created = await activator(state)
                contributions.extend(created)
                for kind, contribution_id in created:
                    await self._save_contribution(state.id, kind, contribution_id)
        except Exception:
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
                except Exception:
                    descriptor = None
                if descriptor is not None and descriptor.origin.value == "plugin":
                    registry.unregister(contribution_id)
            elif kind == "instrument" and self._fabric is not None:
                registry = self._fabric.global_registry
                try:
                    descriptor = registry.resolve(contribution_id)
                except Exception:
                    descriptor = None
                if descriptor is not None and descriptor.origin.value == "plugin":
                    registry.unregister(contribution_id)
            elif kind == "mcp" and self._mcp_adapter is not None:
                self._mcp_adapter.unregister_connection(contribution_id)
                client = self._mcp_clients.pop(contribution_id, None)
                if client is not None:
                    await client.close()
        if remove:
            await self._delete_contributions(state.id)

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
        except Exception:
            for _, skill_id in results:
                try:
                    await self._skill_lifecycle.archive(skill_id)
                except Exception:
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
                    except Exception:
                        self._mcp_adapter.unregister_connection(connection_id)
                        if client is not None:
                            await client.close()
                        raise
        except Exception:
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
                await self._workflow_store.save(workflow)
                saved.append(workflow.id)
        except Exception:
            for workflow_id in saved:
                try:
                    await self._workflow_store.delete(workflow_id)
                except Exception:
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
        except Exception:
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
        except Exception:
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
            return {"pack_id": state.id, "status": "missing", "reason": "install path missing"}
        try:
            _source, manifest, integrity = self._validated_source(str(path))
        except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            return {"pack_id": state.id, "status": "invalid", "reason": str(exc)}
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
    for kind, suffixes in _PROVIDED_FILES.items():
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
        digest.update(path.read_bytes())
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
    except Exception:
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
