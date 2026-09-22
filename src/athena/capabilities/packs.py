"""Governed capability-pack lifecycle."""

from __future__ import annotations
from athena.capabilities.operations import native_descriptor

import json
from typing import Any, Mapping

from athena.packs.manager import PackManager
from athena.protocol.capabilities import (
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)


def _pack_effects(arguments: Mapping[str, Any]) -> frozenset[EffectClass]:
    operation = str(arguments.get("operation") or "").lower()
    effects = {EffectClass.READ_LOCAL}
    if operation in {"install", "upgrade", "install_remote", "enable", "disable"}:
        effects.add(EffectClass.WRITE_LOCAL)
    if operation in {"install", "upgrade", "install_remote", "enable", "disable", "uninstall"}:
        effects.add(EffectClass.PRIVILEGED)
    if operation == "uninstall":
        effects.add(EffectClass.DELETE)
    if operation in {"fetch", "install_remote"}:
        effects.add(EffectClass.NETWORK_READ)
    if operation == "search" and arguments.get("source_url"):
        effects.add(EffectClass.NETWORK_READ)
    return frozenset(effects)


class PacksCapability:
    descriptor = native_descriptor(
        id="packs",
        description=(
            "Inspect and manage declarative Athena capability packs. Packs are "
            "integrity-checked and installed without importing arbitrary code. "
            "Operations: search, inspect, install, upgrade, enable, disable, "
            "uninstall, health."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "search",
                        "fetch",
                        "inspect",
                        "install",
                        "install_remote",
                        "upgrade",
                        "enable",
                        "disable",
                        "uninstall",
                        "health",
                    ],
                },
                "source_path": {"type": "string", "maxLength": 2048},
                "pack_id": {"type": "string", "maxLength": 128},
                "query": {"type": "string", "maxLength": 256},
                "source_url": {"type": "string", "maxLength": 4096},
                "expected_sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"},
                "approved": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        effects=frozenset(
            {
                EffectClass.READ_LOCAL,
                EffectClass.WRITE_LOCAL,
                EffectClass.DELETE,
                EffectClass.NETWORK_READ,
                EffectClass.PRIVILEGED,
            }
        ),
        effect_resolver=_pack_effects,
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, manager: PackManager) -> None:
        self._manager = manager

    async def invoke(self, request: CapabilityRequest, *, context=None, **kwargs):
        del kwargs
        args = dict(request.arguments or {})
        lifecycle = getattr(self._manager, "lifecycle", self._manager)
        operation = str(args.get("operation") or "")
        try:
            if operation == "search":
                source_url = str(args.get("source_url") or "")
                if source_url:
                    rows = self._manager.search_remote(
                        source_url,
                        query=str(args.get("query") or ""),
                        network_policy=_workspace_network_policy(context),
                    )
                    return _result(request, output=json.dumps({"packs": rows}))
                query = str(args.get("query") or "").casefold()
                rows = await self._manager.list()
                if query:
                    rows = [row for row in rows if query in json.dumps(row).casefold()]
                return _result(request, output=json.dumps({"packs": rows}))
            if operation == "fetch":
                source_url = str(args.get("source_url") or "")
                if not source_url:
                    return _result(request, ok=False, error="fetch requires source_url")
                value = self._manager.fetch_remote(
                    source_url,
                    expected_sha256=args.get("expected_sha256"),
                    expected_sha256_source=(
                        "model" if request.origin.value == "model" else "operator"
                    ),
                    network_policy=_workspace_network_policy(context),
                )
                return _result(request, output=json.dumps(value, default=str))
            if operation == "install_remote":
                if request.origin.value not in {"user_direct", "trusted_orchestration", "system"}:
                    return _result(
                        request,
                        ok=False,
                        error="remote pack installation requires operator approval",
                    )
                state = await lifecycle.install_remote(
                    str(args.get("source_url") or ""),
                    expected_sha256=args.get("expected_sha256"),
                    expected_sha256_source=(
                        "model" if request.origin.value == "model" else "operator"
                    ),
                    approved=bool(args.get("approved")),
                    network_policy=_workspace_network_policy(context),
                )
                return _result(request, output=json.dumps(state.to_record()))
            if operation == "inspect":
                source = args.get("source_path")
                if source:
                    value = self._manager.inspect_source(
                        str(source), allowed_root=_workspace_root(context)
                    )
                else:
                    value = await self._manager.inspect_installed(str(args.get("pack_id") or ""))
                return _result(request, output=json.dumps(value, default=str))
            if operation in {"install", "upgrade"}:
                if request.origin.value == "model":
                    return _result(
                        request,
                        ok=False,
                        error="pack installation requires operator promotion",
                    )
                source = str(args.get("source_path") or "")
                if not source:
                    return _result(request, ok=False, error=f"{operation} requires source_path")
                state = (
                    await lifecycle.install(source, allowed_root=_workspace_root(context))
                    if operation == "install"
                    else await lifecycle.upgrade(source, allowed_root=_workspace_root(context))
                )
                return _result(request, output=json.dumps(state.to_record()))
            pack_id = str(args.get("pack_id") or "")
            if not pack_id:
                return _result(request, ok=False, error=f"{operation} requires pack_id")
            if operation == "enable":
                if request.origin.value == "model":
                    return _result(
                        request, ok=False, error="pack activation requires operator promotion"
                    )
                value = (await lifecycle.enable(pack_id)).to_record()
            elif operation == "disable":
                if request.origin.value == "model":
                    return _result(
                        request, ok=False, error="pack deactivation requires operator promotion"
                    )
                value = (await lifecycle.disable(pack_id)).to_record()
            elif operation == "uninstall":
                if request.origin.value == "model":
                    return _result(
                        request, ok=False, error="pack removal requires operator promotion"
                    )
                value = {"pack_id": pack_id, "uninstalled": await lifecycle.uninstall(pack_id)}
            elif operation == "health":
                health = await self._manager.health_for(pack_id)
                if health is None:
                    return _result(request, ok=False, error=f"pack not found: {pack_id}")
                value = health
            else:
                return _result(request, ok=False, error=f"unknown operation: {operation}")
            return _result(request, output=json.dumps(value, default=str))
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return _result(request, ok=False, error=str(exc))


def _workspace_root(context: Any) -> str | None:
    workspace = getattr(context, "workspace", None)
    return getattr(workspace, "root", None)


def _workspace_network_policy(context: Any) -> str | object | None:
    policy = getattr(getattr(context, "workspace", None), "network_policy", None)
    return getattr(policy, "value", policy)


def _result(request, *, ok: bool = True, output: str = "", error: str | None = None):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
    )


__all__ = ["PacksCapability"]
