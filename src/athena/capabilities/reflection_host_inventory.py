"""Host inventory composition for the advisory reflection passport."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from athena.capabilities.reflection_inventory import (
    _available_memory_bytes,
    _command_inventory,
    _git_remote_inventory,
    _graphics_inventory,
    _host_resource_inventory,
    _permission_inventory,
    _port_inventory,
)

__all__ = ["build_host_inventory"]


async def build_host_inventory(
    workspace_root: Path | None,
    workspace_root_value: str | None,
) -> dict[str, Any]:
    """Compose bounded host/repository facts without making policy decisions."""
    return {
        "resources": _host_resource_inventory(workspace_root_value),
        "memory_available_bytes": _available_memory_bytes(),
        "container_engines": _command_inventory(
            ("docker", "podman"), source="PATH:container-engine"
        ),
        "package_managers": _command_inventory(
            ("uv", "pip", "npm", "pnpm", "cargo"), source="PATH:package-manager"
        ),
        "compilers": _command_inventory(
            ("cc", "gcc", "clang", "rustc", "go", "javac"), source="PATH:compiler"
        ),
        "runtimes": _command_inventory(
            ("python", "python3", "node", "deno", "bun"), source="PATH:runtime"
        ),
        "shells": _command_inventory(("sh", "bash", "zsh", "pwsh"), source="PATH:shell"),
        "databases": _command_inventory(
            ("sqlite3", "psql", "mysql", "redis-cli"), source="PATH:database-client"
        ),
        "services": _command_inventory(
            ("systemctl", "docker", "podman"), source="PATH:service-manager"
        ),
        "ports": _port_inventory(),
        "graphics": _graphics_inventory(),
        "permissions": _permission_inventory(workspace_root),
        "gpu": _command_inventory(("nvidia-smi", "rocminfo"), source="PATH:gpu-probe"),
        "git": {
            "workspace_repository": bool(workspace_root and (workspace_root / ".git").exists()),
            "remotes": await _git_remote_inventory(workspace_root),
        },
        "connectivity": {
            "status": "unknown",
            "reason": "connectivity is policy- and route-dependent; no network probe was requested",
        },
        "credential_references": {
            "status": "opaque",
            "source": "operator configuration",
            "values": [],
        },
    }
