"""Pure section builders for the reflection environment passport (item 55).

Each function owns one bounded section of the passport graph. They hold no
state and no authority; `ReflectionCapability` remains the composition point
that assembles the final document.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from athena.execution.environment import ProjectEnvironmentFingerprint
from athena.protocol.tasks import WorkspaceSpec

__all__ = [
    "build_environment_section",
    "explain_capability_sections",
    "build_filesystem_section",
    "build_models_section",
    "build_network_section",
    "build_toolchains_section",
]


def build_filesystem_section(workspace: WorkspaceSpec | None) -> dict[str, Any]:
    """Prove filesystem/sandbox availability for the requesting workspace."""
    workspace_root = Path(workspace.root).resolve() if workspace is not None else None
    sandbox_available = os.name == "posix" and shutil.which("bwrap") is not None
    sandbox_status = (
        "available"
        if sandbox_available
        else ("unsupported" if os.name != "posix" else "unavailable")
    )
    filesystem: dict[str, Any] = {
        "workspace_root": str(workspace_root) if workspace_root else None,
        "workspace_exists": bool(workspace_root and workspace_root.is_dir()),
        "workspace_writable": bool(workspace_root and os.access(workspace_root, os.W_OK)),
        "free_bytes": (
            shutil.disk_usage(workspace_root).free
            if workspace_root and workspace_root.exists()
            else None
        ),
        "sandbox_backend": "bubblewrap" if sandbox_available else None,
        "sandbox_status": sandbox_status,
        "sandbox_remediation": None
        if sandbox_available
        else (
            "restricted execution is supported on POSIX hosts only"
            if os.name != "posix"
            else "install bubblewrap before invoking restricted execution"
        ),
    }
    filesystem_available = bool(
        filesystem["workspace_exists"]
        and filesystem["workspace_writable"]
        and filesystem["sandbox_status"] == "available"
    )
    filesystem["status"] = "available" if filesystem_available else "unavailable"
    filesystem["availability"] = filesystem["status"]
    filesystem["reason"] = (
        None
        if filesystem_available
        else (
            "workspace context is missing or not writable"
            if not filesystem["workspace_exists"] or not filesystem["workspace_writable"]
            else "restricted sandbox backend is unavailable"
        )
    )
    filesystem["remediation"] = (
        None
        if filesystem_available
        else (
            "supply a writable workspace root"
            if not filesystem["workspace_exists"] or not filesystem["workspace_writable"]
            else filesystem["sandbox_remediation"]
        )
    )
    return filesystem


def build_network_section(workspace: WorkspaceSpec | None) -> dict[str, Any]:
    """Classify network posture from policy plus declared physical state."""
    network_policy = (
        getattr(getattr(workspace, "network_policy", None), "value", None)
        if workspace is not None
        else None
    )
    configured_connectivity = "configured" if network_policy is not None else "unknown"
    physical_connectivity = os.environ.get("ATHENA_NETWORK_CONNECTIVITY", "").strip().lower()
    if physical_connectivity not in {"available", "unavailable"}:
        physical_connectivity = "unknown"
    if workspace is None:
        network_state = "unknown"
        network_reason = "no workspace context supplied; physical connectivity is unverified"
    elif network_policy == "deny":
        network_state = "blocked"
        network_reason = "workspace network policy is deny"
    elif network_policy == "restricted":
        network_state = "restricted"
        network_reason = "workspace network policy restricts network access"
    elif physical_connectivity == "available":
        network_state = "available"
        network_reason = None
    else:
        network_state = "unknown"
        network_reason = "workspace policy permits network, but physical connectivity is unverified"
    return {
        "policy": network_policy,
        "status": network_state,
        "configured_connectivity": configured_connectivity,
        "physical_connectivity": physical_connectivity,
        "availability": "available" if network_state == "available" else "unavailable",
        "reason": network_reason,
        "remediation": (
            None
            if network_state == "available"
            else (
                "supply a workspace context before requesting networked work"
                if workspace is None
                else "verify connectivity or set ATHENA_NETWORK_CONNECTIVITY=available"
                if network_state == "unknown"
                else "request an explicit network policy that permits this operation"
            )
        ),
    }


async def build_models_section(model_registry: Any) -> dict[str, Any]:
    """Summarize configured model providers and their readiness."""
    provider_names: list[str] = []
    provider_readiness: dict[str, Any] = {}
    configured_models: list[dict] = []
    reason = "no model provider is configured"
    if model_registry is not None:
        try:
            provider_names = list(model_registry.names())
            readiness_probe = getattr(model_registry, "readiness", None)
            if callable(readiness_probe):
                raw_readiness = readiness_probe()
                if isinstance(raw_readiness, dict) or hasattr(raw_readiness, "items"):
                    provider_readiness = dict(raw_readiness)
            raw_models = model_registry.list_models()
            if hasattr(raw_models, "__await__"):
                raw_models = await raw_models
            models = list(raw_models)
            provider_states = {
                str(name): str(record.get("state") or "unverified")
                for name, record in dict(provider_readiness.get("providers") or {}).items()
                if isinstance(record, dict)
            }
            configured_models = [
                {
                    "id": getattr(model, "id", None),
                    "provider": getattr(model, "provider", None),
                    "context_window": getattr(model, "context_window", None),
                    "status": (
                        "available"
                        if provider_states.get(str(getattr(model, "provider", "")), "ready")
                        == "ready"
                        else "unavailable"
                    ),
                }
                for model in models
            ]
            if not configured_models:
                reason = "configured providers expose no models"
            elif not any(item["status"] == "available" for item in configured_models):
                reason = "configured providers are not ready"
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            reason = f"model inventory unavailable: {exc}"
    return {
        "providers": provider_names,
        "configured": configured_models,
        "provider_readiness": provider_readiness,
        "reason": reason,
        "available": any(item["status"] == "available" for item in configured_models),
    }


def build_toolchains_section() -> list[dict[str, Any]]:
    """Probe the declared development toolchains from PATH."""
    toolchain_names = ("python", "uv", "ruff", "mypy", "pytest", "cargo", "rustc", "node")
    return [
        {
            "name": name,
            "executable": shutil.which(name),
            "status": "available" if shutil.which(name) else "unavailable",
            "reason": None if shutil.which(name) else f"{name} is not installed or not on PATH",
            "remediation": None if shutil.which(name) else f"install or configure {name}",
        }
        for name in toolchain_names
    ]


async def build_environment_section(
    workspace: WorkspaceSpec | None, *, backends: list[Any] | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    """Describe and fingerprint the workspace environment, if any."""
    if workspace is None:
        return None, None
    environment = await ProjectEnvironmentFingerprint().describe_async(
        workspace,
        extras={"backends": backends} if backends else None,
    )
    fingerprint = ProjectEnvironmentFingerprint().digest(environment)
    return environment, fingerprint


async def explain_capability_sections(
    owner,
    *,
    task_id: str | None,
    project_id: str | None,
    user_id: str | None,
    context=None,
) -> list[dict[str, Any]]:
    """Explain availability for every visible capability in one bounded pass."""
    capabilities: list[dict[str, Any]] = []
    for descriptor in owner._fabric.list_descriptors(
        task_id=task_id,
        project_id=project_id,
        user_id=user_id,
    ):
        item = await owner._explain_availability(
            descriptor.id,
            {},
            task_id=task_id,
            project_id=project_id,
            user_id=user_id,
            context=context,
        )
        capabilities.append(
            {
                "id": descriptor.id,
                "status": item["status"],
                "availability": ("available" if item["status"] == "AVAILABLE" else "unavailable"),
                "preconditions": item["preconditions"],
                "checks": item["checks"],
                "reason": item["preconditions"][0] if item["preconditions"] else None,
                "remediation": (
                    "resolve the listed preconditions before invoking"
                    if item["preconditions"]
                    else None
                ),
            }
        )
    return capabilities
