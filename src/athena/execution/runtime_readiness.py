"""Read-only runtime inventory and activity projection."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

__all__ = ["RuntimeReadiness"]


class RuntimeReadiness:
    """Project runtime availability without owning runtime execution."""

    def __init__(
        self,
        runtimes: Callable[[], Mapping[str, Any]],
        task_sessions: Callable[[], Mapping[str, list[tuple[Any, str]]]],
        execution_runtimes: Callable[[], Mapping[str, tuple[Any, str | None]]],
    ) -> None:
        self._runtimes = runtimes
        self._task_sessions = task_sessions
        self._execution_runtimes = execution_runtimes

    def available_runtimes(self) -> list[str]:
        return sorted(self._runtimes().keys())

    def has_runtime(self, name: str) -> bool:
        return name in self._runtimes()

    def status(self) -> list[dict[str, Any]]:
        """Return a health-oriented inventory of registered runtimes."""
        canonical: dict[int, dict[str, Any]] = {}
        for alias, runtime in self._runtimes().items():
            identity = id(runtime)
            entry = canonical.setdefault(
                identity,
                {
                    "id": getattr(runtime, "name", alias),
                    "aliases": [],
                    "available": True,
                    "healthy": True,
                    "persistence": getattr(runtime, "persistence", "unknown"),
                    "active_sessions": 0,
                    "active_executions": 0,
                    "implementation": type(runtime).__name__,
                },
            )
            if alias != entry["id"]:
                entry["aliases"].append(alias)
            availability = getattr(runtime, "available", None)
            if callable(availability):
                try:
                    entry["available"] = bool(availability())
                except Exception:  # noqa: BLE001 - reflection must not crash
                    entry["available"] = False
            entry["healthy"] = entry["available"]
        for sessions in self._task_sessions().values():
            for runtime, _session_id in sessions:
                identity = id(runtime)
                if identity in canonical:
                    canonical[identity]["active_sessions"] += 1
        for execution_runtime, _execution_session_id in self._execution_runtimes().values():
            identity = id(execution_runtime)
            if identity in canonical:
                canonical[identity]["active_executions"] += 1
        for entry in canonical.values():
            entry["aliases"].sort()
        return sorted(canonical.values(), key=lambda item: str(item["id"]))
