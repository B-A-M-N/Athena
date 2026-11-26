"""Composition owner for the read-only environment passport."""

from __future__ import annotations

from collections.abc import Mapping
import inspect
import os
import platform
import sys
from pathlib import Path
from typing import Any

from athena.capabilities.reflection_host_inventory import build_host_inventory
from athena.capabilities.reflection_passport import (
    build_environment_section,
    build_filesystem_section,
    build_models_section,
    build_network_section,
    build_toolchains_section,
    explain_capability_sections,
)
from athena.protocol.tasks import WorkspaceSpec


async def _runtime_sections(
    owner,
    *,
    task_id: str | None,
    project_id: str | None,
    user_id: str | None,
    context,
) -> dict[str, Any]:
    subsystem_health: dict[str, Any] = {}
    if owner._runtime_health is not None:
        try:
            raw_health = owner._runtime_health()
            if inspect.isawaitable(raw_health):
                raw_health = await raw_health
            if isinstance(raw_health, Mapping):
                subsystem_health = {
                    str(name): dict(value) if isinstance(value, Mapping) else value
                    for name, value in raw_health.items()
                }
        except Exception as exc:  # noqa: BLE001 - reflection is advisory
            subsystem_health = {"service_runtime": {"health": "unavailable", "error": str(exc)}}
    capabilities = await explain_capability_sections(
        owner,
        task_id=task_id,
        project_id=project_id,
        user_id=user_id,
        context=context,
    )
    raw_workspace = getattr(context, "workspace", None)
    workspace: WorkspaceSpec | None = (
        raw_workspace if isinstance(raw_workspace, WorkspaceSpec) else None
    )
    backends = []
    if owner._execution is not None:
        status = getattr(owner._execution, "backend_status", None)
        if callable(status):
            backends = list(status())
    backend_records = [
        owner._availability_record(item, kind="execution_backend") for item in backends
    ] or [
        {
            "kind": "execution_backend_provider",
            "status": "unavailable",
            "availability": "unavailable",
            "reason": "no execution backend inventory is configured",
            "remediation": "start the execution manager and register a backend",
        }
    ]
    runtime_records = owner._list_runtimes() or [
        {
            "kind": "runtime_provider",
            "status": "unavailable",
            "availability": "unavailable",
            "reason": "no runtime inventory is configured",
            "remediation": "start the execution manager and register a runtime",
        }
    ]
    environment, environment_fingerprint = await build_environment_section(
        workspace, backends=backends
    )
    models = await build_models_section(owner._models)
    return {
        "subsystems": subsystem_health,
        "capabilities": capabilities,
        "workspace": workspace,
        "backends": backends,
        "backend_records": backend_records,
        "runtime_records": runtime_records,
        "environment": environment,
        "environment_fingerprint": environment_fingerprint,
        "models": models,
        "toolchains": build_toolchains_section(),
        "filesystem": build_filesystem_section(workspace),
        "network": build_network_section(workspace),
    }


async def _secondary_sections(owner, runtime: Mapping[str, Any]) -> dict[str, Any]:
    workspace = runtime["workspace"]
    mcp_status = owner._mcp_status() if callable(owner._mcp_status) else owner._mcp_status
    mcp = []
    for name, value in sorted(dict(mcp_status or {}).items()):
        state = str(value.get("state") or "unknown") if isinstance(value, Mapping) else str(value)
        if isinstance(value, Mapping):
            record = dict(value)
            record.setdefault("id", str(name))
            record["status"] = "available" if state == "connected" else state
            record["availability"] = "available" if state == "connected" else "unavailable"
            record.setdefault(
                "reason",
                None
                if state == "connected"
                else str(value.get("last_error") or "MCP server is not connected"),
            )
            record.setdefault(
                "remediation",
                None
                if state == "connected"
                else "inspect MCP configuration and reconnect the server",
            )
            mcp.append(record)
        else:
            mcp.append(
                {
                    "id": str(name),
                    "status": "available" if state == "connected" else "unavailable",
                    "availability": "available" if state == "connected" else "unavailable",
                    "reason": None if state == "connected" else state,
                    "remediation": None
                    if state == "connected"
                    else "inspect MCP configuration and reconnect the server",
                }
            )
    delegates = owner._delegates() if callable(owner._delegates) else owner._delegates
    delegate_records = (
        [owner._availability_record(item, kind="delegate") for item in delegates.list()]
        if delegates is not None
        else []
    ) or [
        {
            "status": "unavailable",
            "availability": "unavailable",
            "reason": "no host-configured delegate is registered",
            "remediation": "configure a trusted delegate connector",
        }
    ]
    capabilities = runtime["capabilities"]
    toolchains = runtime["toolchains"]
    device_records = [
        owner._availability_record(item, kind="device") for item in owner._list_devices()
    ]
    device_constraints = [
        {
            **item,
            "availability": item["availability"],
            "remediation": item.get("remediation")
            or (
                None
                if item["availability"] == "available"
                else "configure or attach a supported device adapter"
            ),
        }
        for item in device_records
    ]
    platform_record = {
        "os": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "processor": platform.processor() or None,
        "cpu_count": os.cpu_count(),
        "status": "available",
        "availability": "available",
        "reason": None,
        "remediation": None,
    }
    workspace_exists = bool(workspace is not None and Path(workspace.root).is_dir())
    workspace_record = {
        "id": getattr(workspace, "id", None),
        "execution_backend": getattr(workspace, "execution_backend", None),
        "network_policy": getattr(
            getattr(workspace, "network_policy", None),
            "value",
            getattr(workspace, "network_policy", None),
        ),
        "mutation_mode": getattr(
            getattr(workspace, "mutation_mode", None),
            "value",
            getattr(workspace, "mutation_mode", None),
        ),
        "status": "available" if workspace_exists else "unavailable",
        "availability": "available" if workspace_exists else "unavailable",
        "reason": None if workspace_exists else "no workspace context supplied",
        "remediation": None if workspace_exists else "supply an existing workspace root",
    }
    workspace_root = Path(workspace.root).resolve() if workspace is not None else None
    environment = runtime["environment"]
    environment_record = (
        {
            **dict(environment),
            "status": "available",
            "availability": "available",
            "reason": None,
            "remediation": None,
        }
        if environment is not None
        else {
            "status": "unavailable",
            "availability": "unavailable",
            "reason": "no workspace environment could be described",
            "remediation": "supply a workspace context",
        }
    )
    return {
        "mcp": mcp,
        "delegates": delegate_records,
        "generated": [
            item
            for item in capabilities
            if str(item["id"]).startswith("synth_") or str(item["id"]).startswith("generated")
        ],
        "unavailable": [item for item in toolchains if item["status"] == "unavailable"],
        "devices": device_records,
        "device_constraints": device_constraints,
        "platform": platform_record,
        "workspace_record": workspace_record,
        "host_inventory": await build_host_inventory(
            workspace_root,
            workspace.root if workspace else None,
        ),
        "environment_record": environment_record,
    }


def _passport_status(owner, runtime: Mapping[str, Any], secondary: Mapping[str, Any]) -> str:
    models = runtime["models"]
    return (
        "AVAILABLE"
        if all(item["status"] == "AVAILABLE" for item in runtime["capabilities"])
        and models["available"]
        and runtime["filesystem"]["availability"] == "available"
        and secondary["workspace_record"]["availability"] == "available"
        and any(item["availability"] == "available" for item in runtime["backend_records"])
        and any(item["availability"] == "available" for item in runtime["runtime_records"])
        and owner._execution is not None
        and not any(
            isinstance(item, Mapping)
            and str(item.get("health") or "").casefold() in {"degraded", "failed", "unavailable"}
            for item in runtime["subsystems"].values()
        )
        else "PARTIAL"
    )


async def build_environment_passport(
    owner,
    *,
    task_id: str | None,
    project_id: str | None,
    user_id: str | None,
    context=None,
) -> dict[str, Any]:
    """Compose the effective machine/task surface without granting authority."""
    runtime = await _runtime_sections(
        owner,
        task_id=task_id,
        project_id=project_id,
        user_id=user_id,
        context=context,
    )
    secondary = await _secondary_sections(owner, runtime)
    models = runtime["models"]
    return {
        "kind": "environment_passport",
        "status": _passport_status(owner, runtime, secondary),
        "capabilities": runtime["capabilities"],
        "platform": secondary["platform"],
        "host_inventory": secondary["host_inventory"],
        "runtimes": runtime["runtime_records"],
        "backends": runtime["backend_records"],
        "toolchains": runtime["toolchains"],
        "models": {
            "providers": models["providers"],
            "configured": models["configured"],
            **(
                {"provider_readiness": models["provider_readiness"]}
                if models["provider_readiness"]
                else {}
            ),
            "status": "available"
            if models["available"]
            else ("partial" if models["configured"] else "unavailable"),
            "availability": "available" if models["available"] else "unavailable",
            "reason": None if models["available"] else models["reason"],
            "remediation": None
            if models["available"]
            else "configure a provider and model before submitting agent work",
        },
        "mcp": secondary["mcp"],
        "delegates": secondary["delegates"],
        "generated_capabilities": secondary["generated"],
        "dependencies": {
            "status": "available" if not secondary["unavailable"] else "partial",
            "availability": "available" if not secondary["unavailable"] else "unavailable",
            "toolchain_unavailable": [item["name"] for item in secondary["unavailable"]],
            "reason": None
            if not secondary["unavailable"]
            else "one or more declared development/toolchain dependencies are unavailable",
            "remediation": None
            if not secondary["unavailable"]
            else "install the missing tools or use a host with the required toolchain",
        },
        "devices": secondary["devices"],
        "device_constraints": secondary["device_constraints"],
        "network": runtime["network"],
        "filesystem": runtime["filesystem"],
        "environment": secondary["environment_record"],
        "workspace": secondary["workspace_record"],
        "environment_fingerprint": runtime["environment_fingerprint"],
        "subsystems": runtime["subsystems"],
    }
