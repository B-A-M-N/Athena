"""Explicit service resources consumed by readiness/admission checks."""

from __future__ import annotations

from typing import Any


class ReadinessPorts:
    """Allowlist readiness facts and admission seams owned by the service."""

    _RESOURCE_NAMES = frozenset(
        {
            "_require_provider_ready",
            "_normalize_agent_request_acceptance",
            "_admit_model_roles",
            "_required_model_roles",
            "runtime_health",
            "_dispatcher",
            "shadow_engine",
            "_execution",
            "_policy",
            "_reality_coordinator",
            "_project_index_coordinator",
            "_validate_required_capabilities",
        }
    )

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    @property
    def owner(self) -> Any:
        """Narrow application admission boundary."""
        return self._owner

    def __getattr__(self, name: str) -> Any:
        resource_name = self._target(name)
        if resource_name is None:
            raise AttributeError(f"readiness port is not allowed: {name}")
        return getattr(self._owner, resource_name, None)

    @classmethod
    def _target(cls, name: str) -> str | None:
        if name in cls._RESOURCE_NAMES:
            return name
        candidate = f"_{name}"
        return candidate if candidate in cls._RESOURCE_NAMES else None


__all__ = ["ReadinessPorts"]
