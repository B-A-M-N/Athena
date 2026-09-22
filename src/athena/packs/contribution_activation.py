"""Declarative pack contribution activation.

Each contribution kind is admitted against the pack's manifest authority and
re-enters an existing canonical Athena surface. Pack code is never loaded.
"""

from __future__ import annotations

import hashlib
import json
from importlib import import_module
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from athena.packs.models import PackState
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

try:
    tomllib = import_module("tomllib")
except ModuleNotFoundError:  # pragma: no cover - legacy/minimal Python builds
    tomllib = import_module("tomli")


async def activate_pack_skills(host: Any, state: PackState) -> list[tuple[str, str]]:
    if host._skill_lifecycle is None:
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
            skill_id = f"pack:{state.id}:skill:{hashlib.sha256(relative.encode()).hexdigest()[:16]}"
            skill = replace(
                skill,
                id=skill_id,
                metadata={
                    **dict(skill.metadata),
                    "pack_id": state.id,
                    "pack_version": state.manifest.version,
                },
            )
            await host._skill_lifecycle.install(skill)
            results.append(("skill", skill_id))
    except Exception:  # noqa: BLE001 - roll back installed skills, then preserve failure
        for _, skill_id in results:
            try:
                await host._skill_lifecycle.archive(skill_id)
            except Exception:  # noqa: BLE001 - rollback is best effort after failed activation
                pass
        raise
    return results


async def activate_pack_mcp_servers(host: Any, state: PackState) -> list[tuple[str, str]]:
    """Connect declarative pack MCP servers through the existing adapter."""
    files = state.manifest.provides.get("mcp_servers", ())
    if not files:
        return []
    if host._mcp_adapter is None:
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
                client_factory = getattr(host, "_mcp_client_factory", None)
                if client_factory is None:
                    from athena.mcp.client import MCPClient

                    client_factory = MCPClient
                client: Any = None
                try:
                    client = client_factory(
                        connection_id,
                        command=str(command) if command is not None else None,
                        args=[str(item) for item in record.get("args") or ()],
                        url=str(url) if url is not None else None,
                        env={
                            str(key): str(value) for key, value in (record.get("env") or {}).items()
                        },
                        connect_timeout=float(record.get("connect_timeout", 10.0)),
                    )
                    await client.connect()
                    descriptors = await host._mcp_adapter.collect_and_register(
                        client,
                        server_alias=connection_id,
                    )
                    effects = {
                        effect.value for descriptor in descriptors for effect in descriptor.effects
                    }
                    if not effects.issubset(requested):
                        raise ValueError(
                            f"pack MCP server {name!r} exceeds requested effects: "
                            + ", ".join(sorted(effects - requested))
                        )
                    host._activation_state.mcp_clients[connection_id] = client
                    if host._mcp_client_sink is not None:
                        host._mcp_client_sink(client)
                    created.append(("mcp", connection_id))
                except Exception:  # noqa: BLE001 - clean up failed MCP admission
                    host._mcp_adapter.unregister_connection(connection_id)
                    if client is not None:
                        await client.close()
                    raise
    except Exception:  # noqa: BLE001 - roll back installed workflows, then preserve failure
        for _, connection_id in created:
            if host._mcp_adapter is not None:
                host._mcp_adapter.unregister_connection(connection_id)
            client = host._activation_state.mcp_clients.pop(connection_id, None)
            if client is not None:
                await client.close()
        raise
    return created


async def activate_pack_workflows(host: Any, state: PackState) -> list[tuple[str, str]]:
    if host._workflow_store is None:
        return []
    from athena.protocol.affordances import AffordanceScope
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
        if host._fabric is None:
            raise ValueError(f"pack workflow dependency unavailable: {identifier}")
        return host._fabric.global_registry.resolve(identifier)

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
            await host._workflow_store.save(workflow)
            saved.append(workflow.id)
    except Exception:  # noqa: BLE001 - roll back saved workflows
        for workflow_id in saved:
            try:
                await host._workflow_store.delete(workflow_id)
            except Exception:  # noqa: BLE001 - rollback is best effort after failed activation
                pass
        raise
    return [("workflow", workflow.id) for workflow in workflows]


async def activate_pack_instruments(host: Any, state: PackState) -> list[tuple[str, str]]:
    """Expose pack-declared instrument views through governed aliases.

    An instrument contribution is intentionally a capability alias rather
    than an out-of-band UI callback.  Invoking it still enters the normal
    dispatcher and the result carries a bounded ``InstrumentView`` record.
    Pack data can therefore add a useful surface without loading code or
    bypassing policy.
    """
    if host._fabric is None or host._dispatcher is None:
        return []
    from athena.protocol.instruments import InstrumentView

    registry = host._fabric.global_registry
    created: list[str] = []
    try:
        for relative in state.manifest.provides.get("instruments", ()):
            value = json.loads((Path(state.install_path) / relative).read_text(encoding="utf-8"))
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
                    dispatcher=host._dispatcher,
                    defaults=dict(record.get("defaults") or {}),
                    input_schema=dict(record.get("input_schema") or target.descriptor.input_schema),
                    view=view,
                )
                registry.register(alias, authority=f"pack:{state.id}")
                created.append(alias_id)
    except Exception:  # noqa: BLE001 - roll back registered instruments
        for alias_id in created:
            registry.unregister(alias_id)
        raise
    return [("instrument", alias_id) for alias_id in created]


async def activate_pack_capabilities(host: Any, state: PackState) -> list[tuple[str, str]]:
    """Activate only declarative aliases to existing capabilities."""
    if host._fabric is None or host._dispatcher is None:
        return []
    registry = host._fabric.global_registry
    results: list[tuple[str, str]] = []
    try:
        for relative in state.manifest.provides.get("capabilities", ()):
            value = json.loads((Path(state.install_path) / relative).read_text(encoding="utf-8"))
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
                    dispatcher=host._dispatcher,
                    defaults=dict(record.get("defaults") or {}),
                    input_schema=dict(record.get("input_schema") or target.descriptor.input_schema),
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
        from athena.protocol.continuations import SuspendedCall
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


__all__ = [
    "activate_pack_capabilities",
    "activate_pack_instruments",
    "activate_pack_mcp_servers",
    "activate_pack_skills",
    "activate_pack_workflows",
]
