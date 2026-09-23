"""Backend catalog and health inventory for the execution manager.

Subordinate to :class:`athena.execution.manager.ExecutionManager`. This module
owns the read-only inventory surface: listing backends, health/probe status,
and capability descriptors. It does not select the backend for an execution
(that stays on the manager), and it does not authorize or run anything.
"""

from __future__ import annotations

from typing import Any, Mapping

from athena.execution.backend import ExecutionBackend
from athena.execution.conformance import passport_binding_errors

__all__ = ["BackendCatalog"]


class BackendCatalog:
    """Track registered backends and expose health/capability inventory."""

    def __init__(self) -> None:
        self._backends: dict[str, ExecutionBackend] = {}
        self._passports: dict[str, dict[str, Any]] = {}

    def register(self, backend: ExecutionBackend) -> None:
        name = getattr(backend, "name", type(backend).__name__)
        self._backends[name] = backend

    @property
    def backends(self) -> dict[str, ExecutionBackend]:
        return self._backends

    def set_passport(
        self,
        backend: str,
        passport: Mapping[str, Any],
        *,
        expected_release_sha: str | None = None,
        expected_release_run_id: str | None = None,
        expected_environment: Mapping[str, Any] | None = None,
    ) -> None:
        record = dict(passport)
        errors = passport_binding_errors(
            record,
            backend=backend,
            expected_release_sha=expected_release_sha,
            expected_release_run_id=expected_release_run_id,
            expected_environment=expected_environment,
        )
        record["binding_status"] = "verified" if not errors else "unverified"
        record["binding_errors"] = list(errors)
        self._passports[backend] = record

    def get_passport(self, name: str) -> dict[str, Any] | None:
        return self._passports.get(name)

    def get(self, name: str) -> ExecutionBackend | None:
        return self._backends.get(name)

    def _proof_fields(self, name: str) -> dict[str, Any]:
        passport = self._passports.get(name)
        status = str(passport.get("status") or "") if passport else ""
        binding_status = str(passport.get("binding_status") or "") if passport else ""
        return {
            "proof_status": "verified" if status == "PASS" and binding_status == "verified" else "unverified",
            "passport_status": status or None,
            "passport_binding_status": binding_status or None,
            "passport_binding_errors": list(passport.get("binding_errors") or ()) if passport else [],
        }

    def names(self) -> list[str]:
        return sorted(self._backends)

    def status(
        self,
        *,
        local_backend: ExecutionBackend | None = None,
        available_runtimes: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return availability for the full backend inventory."""
        runtimes = tuple(available_runtimes or ())
        local_available = bool(runtimes)
        result = [
            {
                "id": "local",
                "physical_backend": getattr(local_backend, "name", "local")
                if local_backend is not None
                else "local-runtime-manager",
                "recognized": True,
                "available": local_available,
                "healthy": local_available,
                "runtime_lifetime": "athena_process",
                "reattach": False,
                "network_modes": ["allow", "deny"],
                "network_policy_effects": {
                    "allow": "allow",
                    "deny": "deny",
                    "restricted": "deny",
                },
                "runtimes": list(runtimes),
                **self._proof_fields(
                    getattr(local_backend, "name", "local")
                    if local_backend is not None
                    else "local"
                ),
            }
        ]
        if local_backend is not None:
            result[0]["implementation"] = type(local_backend).__name__
            probe = getattr(local_backend, "available", None)
            if callable(probe):
                try:
                    result[0]["available"] = bool(probe()) and local_available
                except Exception:  # rationale: failed availability probe is unhealthy
                    result[0]["available"] = False
                result[0]["healthy"] = result[0]["available"]
            passport = self._passports.get(getattr(local_backend, "name", "local"))
            if passport is not None:
                result[0]["passport"] = dict(passport)
            try:
                value = local_backend.capabilities()
                result[0].update(
                    {
                        "runtime_lifetime": getattr(
                            value, "runtime_lifetime", "athena_process"
                        ),
                        "reattach": bool(getattr(value, "reattach", False)),
                        "network_modes": list(getattr(value, "network_modes", ()) or ()),
                        "network_policy_effects": dict(
                            getattr(value, "network_policy_effects", {}) or {}
                        ),
                    }
                )
                result[0]["capabilities"] = {
                    key: list(item) if isinstance(item, tuple) else item
                    for key, item in vars(value).items()
                }
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                result[0]["capabilities_error"] = str(exc)
        for name, backend in sorted(self._backends.items()):
            available = True
            probe = getattr(backend, "available", None)
            if callable(probe):
                try:
                    available = bool(probe())
                except Exception:  # rationale: failed availability probe is unhealthy
                    available = False
            result.append(
                {
                    "id": name,
                    "physical_backend": name,
                    "recognized": True,
                    "available": available,
                    "healthy": available,
                    "implementation": type(backend).__name__,
                    **self._proof_fields(name),
                }
            )
            passport = self._passports.get(name)
            if passport is not None:
                result[-1]["passport"] = dict(passport)
            capabilities = getattr(backend, "capabilities", None)
            if callable(capabilities):
                try:
                    value = capabilities()
                    result[-1]["capabilities"] = {
                        key: list(item) if isinstance(item, tuple) else item
                        for key, item in vars(value).items()
                    }
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    result[-1]["capabilities_error"] = str(exc)
            identity = getattr(backend, "environment_identity", None)
            if available and callable(identity):
                try:
                    result[-1].update(dict(identity()))
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    result[-1]["environment_identity_error"] = str(exc)
        return result
