"""Runtime and backend routing mechanics subordinate to ``ExecutionManager``.

This object owns registries and deterministic lookup only. It does not execute
work, persist receipts, apply policy, or decide task strategy; those remain
with ``ExecutionManager`` and the canonical policy path.
"""

from __future__ import annotations

from typing import Any, Callable

from athena.execution.backend import ExecutionBackend
from athena.execution.backend_catalog import BackendCatalog
from athena.protocol.execution import Runtime

__all__ = ["ExecutionRouting"]


class ExecutionRouting:
    """Registry and resolver for manager-owned runtimes and backends."""

    def __init__(self) -> None:
        self.runtimes: dict[str, Runtime] = {}
        self.catalog = BackendCatalog()
        self.local_backend: ExecutionBackend | None = None

    @property
    def backends(self) -> dict[str, ExecutionBackend]:
        return self.catalog.backends

    def set_local_backend(self, backend: ExecutionBackend | None) -> None:
        self.local_backend = backend

    def set_backend_passport(
        self,
        backend: str,
        passport: dict[str, Any],
        *,
        expected_release_sha: str | None = None,
        expected_release_run_id: str | None = None,
        expected_environment: dict[str, Any] | None = None,
    ) -> None:
        if passport.get("kind") != "athena_backend_passport":
            raise ValueError("backend passport has an unsupported kind")
        if str(passport.get("backend") or "") != str(backend):
            raise ValueError("backend passport identity does not match backend")
        self.catalog.set_passport(
            str(backend),
            passport,
            expected_release_sha=expected_release_sha,
            expected_release_run_id=expected_release_run_id,
            expected_environment=expected_environment,
        )

    def register_runtime(self, runtime: Runtime) -> None:
        name = getattr(runtime, "name", None)
        if not name:
            raise ValueError("runtime must define a non-empty 'name'")
        self.runtimes[name] = runtime
        for alias in getattr(runtime, "aliases", ()) or ():
            self.runtimes.setdefault(alias, runtime)

    def register_backend(self, backend: ExecutionBackend) -> None:
        """Register a non-local backend for manager-mediated execution."""
        name = getattr(backend, "name", None)
        if not name:
            raise ValueError("backend must define a non-empty 'name'")
        if name == "local":
            raise ValueError("local is the manager's built-in backend")
        self.catalog.register(backend)

    def backend_for_reattach(self, name: str) -> ExecutionBackend | None:
        """Return a registered backend eligible to prove session ownership."""
        if self.local_backend is not None and name == getattr(self.local_backend, "name", None):
            return self.local_backend
        return self.backends.get(name)

    def select_backend(
        self,
        name: str,
        *,
        available_backends: Callable[[], list[str]],
    ) -> ExecutionBackend | None:
        if name == "local" and self.local_backend is not None:
            return self.local_backend
        if name in {"local", "sandboxed-local", "shadow", "sandbox", "verification"}:
            return None
        backend = self.backends.get(name)
        if backend is None:
            raise RuntimeError(
                f"no such execution backend: {name!r}; registered: {available_backends()}"
            )
        return backend

    def resolve_runtime(
        self,
        name: str,
        *,
        available_runtimes: Callable[[], list[str]],
    ) -> Runtime:
        runtime = self.runtimes.get(name)
        if runtime is None:
            raise RuntimeError(f"no such runtime: {name!r}; registered: {available_runtimes()}")
        return runtime

    def resolve_runtime_by_execution(
        self,
        execution_id: str,
        *,
        executions: dict[str, str],
        execution_runtimes: dict[str, tuple[Any, str | None]],
        task_sessions: dict[str, list[tuple[Any, str]]],
    ) -> Runtime | Any | None:
        task_id = executions.get(execution_id)
        if task_id is None:
            return None
        entry = execution_runtimes.get(execution_id)
        if entry is not None:
            return entry[0]
        for runtime, _session_id in task_sessions.get(task_id, []):
            return runtime
        return next(iter(self.runtimes.values()), None)
