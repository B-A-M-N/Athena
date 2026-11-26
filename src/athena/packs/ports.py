"""Explicit dependency ports for extracted pack mechanisms.

These dataclasses replace whole-host injection: extracted mechanisms receive
exactly the capabilities they need instead of reaching through a host
reference into owner internals (review item 29).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PackInstallPorts:
    """Capabilities required by PackInstaller."""

    store: Any
    validated_source: Any
    root: Path
    integrations_bound: bool
    hook_outbox: Any | None
    remove_installed_path: Any
    activate: Any
    deactivate: Any
    health: Any
    fetch_remote: Any


@dataclass(frozen=True)
class PackActivationPorts:
    """Capabilities required by PackActivator."""

    contributions: Any
    save_contribution: Any
    delete_contributions: Any
    activate_skills: Any
    activate_workflows: Any
    activate_capabilities: Any
    activate_instruments: Any
    activate_mcp_servers: Any
    activate_hooks: Any
    workflow_store: Any
    skill_lifecycle: Any
    fabric: Any
    event_store: Any
    hook_callbacks: Any
    hook_contracts: Any
    mcp_adapter: Any
    mutable: "PackMutableActivationState"


@dataclass
class PackMutableActivationState:
    """Mutable activation state shared with PackManager."""

    hook_callbacks: dict[str, list[tuple[str, Any]]] = field(default_factory=dict)
    hook_contracts: dict[str, dict[str, Any]] = field(default_factory=dict)
    mcp_clients: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PackHookPorts:
    """Capabilities required by PackHookRuntime.

    ``state`` is the explicit shared mutable runtime state; the granular
    legacy fields remain only until all call sites are migrated.
    """

    hook_callbacks: Any
    hook_contracts: Any
    hook_health: Any
    hook_outbox: Any | None
    hook_retry_task: Any
    workflow_store: Any
    state: Any | None = None


@dataclass
class PackLegacyHookSlots:
    """Mutable fallback slots for legacy hook hosts.

    Production construction supplies :class:`PackRuntimeState`; this adapter
    remains only until every legacy construction is migrated.
    """

    hook_retry_task: Any | None = None


__all__ = [
    "PackInstallPorts",
    "PackActivationPorts",
    "PackHookPorts",
    "PackLegacyHookSlots",
    "PackMutableActivationState",
]
