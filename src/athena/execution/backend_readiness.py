"""Read-only backend readiness surface for :mod:`execution.manager`.

This helper reports the backend inventory and effective preflight status.  It
does not execute work or choose a second execution path; the manager supplies
the canonical backend resolver used by the readiness queries.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from athena.execution.backend import ExecutionBackend
from athena.execution.backend_catalog import BackendCatalog

__all__ = ["BackendReadiness"]


class BackendReadiness:
    """Expose backend inventory and readiness through manager-owned ports."""

    def __init__(
        self,
        catalog: BackendCatalog,
        *,
        local_backend: Callable[[], ExecutionBackend | None],
        available_runtimes: Callable[[], list[str]],
        selected_backend: Callable[[str], ExecutionBackend | None],
        available_backends: Callable[[], list[str]],
    ) -> None:
        self._catalog = catalog
        self._local_backend = local_backend
        self._available_runtimes = available_runtimes
        self._selected_backend = selected_backend
        self._available_backends = available_backends

    def available_backends(self) -> list[str]:
        return ["local", "sandboxed-local", *self._catalog.names()]

    def backend_status(self) -> list[dict[str, Any]]:
        return self._catalog.status(
            local_backend=self._local_backend(),
            available_runtimes=self._available_runtimes(),
        )

    def backend_capabilities(self, name: str = "local") -> Any:
        selected = self._selected_backend(name)
        if selected is not None:
            return selected.capabilities()
        if name in {"local", "sandboxed-local"}:
            runtimes = tuple(self._available_runtimes())
            return {
                "supported_runtimes": runtimes,
                "dependency_installation": tuple(
                    manager
                    for manager, runtime in (("python", "python"), ("node", "node"))
                    if runtime in runtimes
                ),
            }
        raise ValueError(f"unknown execution backend: {name!r}")

    def backend(self, name: str) -> ExecutionBackend | None:
        """Return the selected backend for structured backend RPCs."""
        return self._selected_backend(name)

    def resolve_backend(self, name: str | None = None) -> object:
        """Resolve the canonical backend identity using execution semantics."""
        canonical = name or "local"
        selected = self._selected_backend(canonical)
        if selected is not None:
            return selected
        if canonical in {"local", "sandboxed-local", "shadow", "sandbox", "verification"}:
            return {"backend": canonical, "available": True, "runtime": "local"}
        raise RuntimeError(
            f"no such execution backend: {canonical!r}; registered: {self._available_backends()}"
        )

    async def check_backend(self, name: str | None = None) -> dict[str, Any]:
        """Prove effective availability for the backend execution would use."""
        resolved = self.resolve_backend(name)
        if isinstance(resolved, dict):
            return resolved
        available = True
        detail = ""
        try:
            probe = getattr(resolved, "available", None)
            if callable(probe):
                available = bool(probe())
        except Exception as exc:  # noqa: BLE001 - readiness reports the failure
            available = False
            detail = str(exc)
        return {
            "backend": getattr(resolved, "name", name or "local"),
            "available": available,
            "implementation": type(resolved).__name__,
            **({"error": detail} if detail else {}),
        }
