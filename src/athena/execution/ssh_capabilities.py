"""Static capability contract for the governed SSH backend."""

from __future__ import annotations

from athena.execution.backend import BackendCapabilities

__all__ = ["ssh_capabilities"]


def ssh_capabilities() -> BackendCapabilities:
    """Describe the authenticated remote supervisor contract."""
    return BackendCapabilities(
        supported_runtimes=("node", "python", "shell"),
        persistent_sessions=True,
        reattach=True,
        filesystem_persistence=True,
        network_modes=("allow",),
        network_policy_effects={"allow": "allow"},
        secret_materialization=True,
        interactive_stdin=True,
        process_signals=True,
        dependency_installation=("python", "node"),
        runtime_lifetime="remote_supervisor",
        runtime_capabilities={
            runtime: {
                "persistent_sessions": True,
                "persistent_runtime_state": True,
                "reattach": True,
                "secret_materialization": True,
                "interactive_stdin": True,
                "process_signals": True,
            }
            for runtime in ("python", "node", "shell")
        },
    )
