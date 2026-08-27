"""Execution backends (BUILDSPEC 51).

A backend abstracts *where* runtimes run: initially ``local`` (in-process,
owning subprocesses on the host) and ``container`` (docker). The contract
matches ``ExecutionBackend`` in ``athena.protocol.execution``.

``ExecutionBackend`` is an abstract base; a registry maps backend names to
instances. All process execution still flows through ExecutionManager (INV-005).
"""

from __future__ import annotations

import abc
from typing import AsyncIterator, Mapping

from athena.protocol.execution import ExecutionEvent, ExecutionRequest

__all__ = ["ExecutionBackend", "BackendRegistry", "register_backend", "get_backend"]


class ExecutionBackend(abc.ABC):
    name: str = ""

    @abc.abstractmethod
    async def create_session(
        self,
        *,
        task_id: str,
        runtime: str,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        workspace_root: str | None = None,
        network_policy: str | None = None,
    ) -> str:
        """Create a persistent runtime session scoped to ``task_id``."""

    @abc.abstractmethod
    def execute(self, request: ExecutionRequest) -> AsyncIterator[ExecutionEvent]:
        """Run ``request`` on its target runtime, streaming events."""

    @abc.abstractmethod
    async def interrupt(self, execution_id: str) -> None:
        """Ask the running runtime to interrupt ``execution_id``."""

    @abc.abstractmethod
    async def destroy_session(self, runtime_session_id: str) -> None:
        """Tear down a runtime session and its owned process tree."""

    @abc.abstractmethod
    async def shutdown(self) -> None:
        """Close every session owned by this backend."""


class BackendRegistry:
    def __init__(self) -> None:
        self._backends: dict[str, ExecutionBackend] = {}

    def register(self, backend: ExecutionBackend) -> None:
        if not backend.name:
            raise ValueError("backend must define non-empty 'name'")
        self._backends[backend.name] = backend

    def get(self, name: str) -> ExecutionBackend:
        backend = self._backends.get(name)
        if backend is None:
            raise RuntimeError(
                f"no such backend: {name!r}; available: {sorted(self._backends)}"
            )
        return backend

    def available(self) -> list[str]:
        return sorted(self._backends)


_registry = BackendRegistry()


def register_backend(backend: ExecutionBackend) -> None:
    _registry.register(backend)


def get_backend(name: str) -> ExecutionBackend:
    return _registry.get(name)
