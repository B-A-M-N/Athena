"""Read-only backend readiness surface for :mod:`execution.manager`.

This helper reports the backend inventory and effective preflight status.  It
does not execute work or choose a second execution path; the manager supplies
the canonical backend resolver used by the readiness queries.
"""

from __future__ import annotations

from collections.abc import Callable
import os
import shutil
from typing import Any

from athena.execution.backend import ExecutionBackend
from athena.execution.backend_catalog import BackendCatalog

__all__ = ["BackendReadiness"]


class BackendReadiness:
    """Expose backend inventory and readiness through manager-owned ports."""

    _BUILTIN_ALIASES = (
        "local",
        "sandboxed-local",
        "shadow",
        "sandbox",
        "verification",
    )
    _ISOLATED_ALIASES = {"sandboxed-local", "shadow", "sandbox", "verification"}

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
        return [*self._BUILTIN_ALIASES, *self._catalog.names()]

    def backend_status(self) -> list[dict[str, Any]]:
        result = self._catalog.status(
            local_backend=self._local_backend(),
            available_runtimes=self._available_runtimes(),
        )
        local_row = next((item for item in result if item.get("id") == "local"), None)
        if local_row is not None and "capabilities" not in local_row:
            local_row["capabilities"] = self.backend_capabilities("local")
        for alias in self._BUILTIN_ALIASES:
            if alias == "local":
                continue
            status = self._builtin_status(alias)
            status["capabilities"] = self.backend_capabilities(alias)
            status["id"] = alias
            result.append(status)
        return result

    def backend_capabilities(self, name: str = "local") -> Any:
        selected = self._selected_backend(name)
        if selected is not None:
            return selected.capabilities()
        if name in self._BUILTIN_ALIASES:
            status = self._builtin_status(name)
            runtimes = tuple(status["runtimes"])
            return {
                "supported_runtimes": runtimes,
                "physical_backend": status["physical_backend"],
                "isolation_required": status["isolation_required"],
                "isolation_verified": status["isolation_verified"],
                "network_modes": ("allow", "deny"),
                "network_policy_effects": {
                    "allow": "allow",
                    "deny": "deny",
                    "restricted": "deny",
                },
                "runtime_lifetime": status["runtime_lifetime"],
                "reattach": False,
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
        if canonical in self._BUILTIN_ALIASES:
            return self._builtin_status(canonical)
        raise RuntimeError(
            f"no such execution backend: {canonical!r}; registered: {self._available_backends()}"
        )

    def _builtin_status(self, name: str) -> dict[str, Any]:
        runtimes = tuple(self._available_runtimes())
        isolated = name in self._ISOLATED_ALIASES
        bwrap_available = os.name != "nt" and shutil.which("bwrap") is not None
        available = bool(runtimes) and (not isolated or bwrap_available)
        if not runtimes:
            reason = "no concrete runtimes are registered"
        elif isolated and not bwrap_available:
            reason = "bubblewrap is unavailable for the requested isolation boundary"
        else:
            reason = ""
        return {
            "backend": name,
            "physical_backend": "local-runtime-manager",
            "runtime": "local",
            "recognized": True,
            "available": available,
            "proof_status": "unverified",
            "isolation_required": isolated,
            "isolation_verified": bool(available and isolated),
            "network_modes": ["allow", "deny"],
            "network_policy_effects": {
                "allow": "allow",
                "deny": "deny",
                "restricted": "deny",
            },
            "runtime_lifetime": "athena_process",
            "reattach": False,
            "runtimes": list(runtimes),
            **({"error": reason} if reason else {}),
        }

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
            "recognized": True,
            "available": available,
            "proof_status": (
                "verified"
                if (self._catalog.get_passport(getattr(resolved, "name", name or "local")) or {}).get(
                    "status"
                )
                == "PASS"
                else "unverified"
            ),
            "implementation": type(resolved).__name__,
            **({"error": detail} if detail else {}),
        }
