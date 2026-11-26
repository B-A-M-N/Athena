"""Read-only live health projection for the service facade."""

from __future__ import annotations

from typing import Any

from athena.service.health_ports import HealthPorts


def build_runtime_health(service: Any) -> dict[str, Any]:
    """Assemble operator health from service-owned live subsystem facts."""
    ports = HealthPorts(service)
    scheduler = ports.scheduler
    watches = ports.watch_registry
    computer = ports.computer
    browser = ports.browser
    memory = ports.memory
    model_registry = ports.model_registry
    model_readiness = (
        model_registry.readiness()
        if model_registry is not None and callable(getattr(model_registry, "readiness", None))
        else {"state": "unconfigured", "providers": {}}
    )
    embedding_provider = getattr(memory, "embedding_provider", None) if memory is not None else None
    embedding_health = (
        embedding_provider.health()
        if embedding_provider is not None and callable(getattr(embedding_provider, "health", None))
        else {
            "configured": False,
            "available": False,
            "state": "unconfigured",
            "reason": "no embedding provider configured",
        }
    )
    mcp = ports.mcp_status()
    capability_profile = ports.live_capability_profile_status(mcp)
    return {
        "scheduler": scheduler.health() if scheduler is not None else {"health": "stopped"},
        "watch": watches.health() if watches is not None else {"health": "stopped"},
        "computer": computer.health() if computer is not None else dict(ports.computer_health),
        "browser": browser.health() if browser is not None else dict(ports.browser_health),
        "memory_embeddings": embedding_health,
        "model": {
            "state": model_readiness.get("state", "unknown"),
            "configured": bool(model_readiness.get("providers")),
            "providers": model_readiness.get("providers", {}),
            "reason": (
                None if model_readiness.get("state") == "ready" else "no ready model provider"
            ),
        },
        "execution_recovery": {
            "state": ports.recovery_status,
            "summary": dict(ports.recovery_summary),
            "error": ports.recovery_error,
        },
        "provider_outcome_recovery": dict(ports.provider_recovery_health),
        "mcp": mcp,
        "capability_profile": capability_profile,
        "optional_capabilities": {
            name: dict(value) for name, value in sorted(ports.optional_capability_health.items())
        },
        "resources": (
            ports.resource_finalizer.health()
            if ports.resource_finalizer is not None
            else {"state": "not_started"}
        ),
        "shutdown": dict(ports.shutdown_status),
    }


__all__ = ["build_runtime_health"]
