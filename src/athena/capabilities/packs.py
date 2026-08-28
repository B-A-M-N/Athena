"""Governed capability-pack lifecycle."""

from __future__ import annotations

import json
from typing import Any

from athena.packs.manager import PackManager
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)


class PacksCapability:
    descriptor = CapabilityDescriptor(
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
                        "inspect",
                        "install",
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
            },
            "additionalProperties": False,
        },
        effects=frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL, EffectClass.DELETE}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, manager: PackManager) -> None:
        self._manager = manager

    async def invoke(self, request: CapabilityRequest, *, context=None, **kwargs):
        del kwargs
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")
        try:
            if operation == "search":
                query = str(args.get("query") or "").casefold()
                rows = await self._manager.list()
                if query:
                    rows = [row for row in rows if query in json.dumps(row).casefold()]
                return _result(request, output=json.dumps({"packs": rows}))
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
                source = str(args.get("source_path") or "")
                if not source:
                    return _result(request, ok=False, error=f"{operation} requires source_path")
                state = (
                    await self._manager.install(source, allowed_root=_workspace_root(context))
                    if operation == "install"
                    else await self._manager.upgrade(source, allowed_root=_workspace_root(context))
                )
                return _result(request, output=json.dumps(state.to_record()))
            pack_id = str(args.get("pack_id") or "")
            if not pack_id:
                return _result(request, ok=False, error=f"{operation} requires pack_id")
            if operation == "enable":
                value = (await self._manager.enable(pack_id)).to_record()
            elif operation == "disable":
                value = (await self._manager.disable(pack_id)).to_record()
            elif operation == "uninstall":
                value = {"pack_id": pack_id, "uninstalled": await self._manager.uninstall(pack_id)}
            elif operation == "health":
                state = await self._manager._store.get(pack_id)  # noqa: SLF001
                if state is None:
                    return _result(request, ok=False, error=f"pack not found: {pack_id}")
                value = self._manager.health(state)
            else:
                return _result(request, ok=False, error=f"unknown operation: {operation}")
            return _result(request, output=json.dumps(value, default=str))
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return _result(request, ok=False, error=str(exc))


def _workspace_root(context: Any) -> str | None:
    workspace = getattr(context, "workspace", None)
    return getattr(workspace, "root", None)


def _result(request, *, ok: bool = True, output: str = "", error: str | None = None):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
    )


__all__ = ["PacksCapability"]
