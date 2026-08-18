"""Container execution backend (BUILDSPEC 51, initial backends: local, container).

Requires the optional ``docker`` dependency. Provides a thin ``docker exec``
wrapper: a stateless container per execution. If ``docker`` is unavailable the
backend is importable but every operation raises ``NotImplementedError`` so the
interface always exists (BUILDSPEC 9 structure).
"""

from __future__ import annotations

from typing import AsyncIterator, Mapping

from athena.execution.backend import ExecutionBackend
from athena.protocol.execution import ExecutionEvent, ExecutionRequest

__all__ = ["ContainerBackend"]

try:
    import docker  # type: ignore
    _DOCKER_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    docker = None  # type: ignore[assignment]
    _DOCKER_AVAILABLE = False


class ContainerBackend(ExecutionBackend):
    name = "container"

    def __init__(self, image: str = "python:3.13-slim") -> None:
        self.image = image
        self._client = None
        if _DOCKER_AVAILABLE:
            try:
                self._client = docker.from_env()
            except Exception:  # pragma: no cover - daemon not reachable
                self._client = None

    def available(self) -> bool:
        return self._client is not None

    def _require(self) -> None:
        if not self.available():
            raise NotImplementedError(
                "docker container backend unavailable: install the 'docker' "
                "package and ensure the daemon is reachable"
            )

    async def create_session(
        self,
        *,
        task_id: str,
        runtime: str,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> str:
        self._require()
        return f"container_{runtime}_{task_id}"

    async def execute(self, request: ExecutionRequest) -> AsyncIterator[ExecutionEvent]:
        self._require()
        raise NotImplementedError("container execute not yet implemented")
        yield  # unreachable; keeps this an async generator per the contract

    async def interrupt(self, execution_id: str) -> None:
        self._require()
        raise NotImplementedError("container interrupt not yet implemented")

    async def destroy_session(self, runtime_session_id: str) -> None:
        self._require()
        raise NotImplementedError("container destroy_session not yet implemented")

    async def shutdown(self) -> None:
        self._require()
        raise NotImplementedError("container shutdown not yet implemented")


def available_backends() -> dict[str, bool]:
    """Return a map of backend names to availability."""
    return {
        "local": True,
        "container": ContainerBackend().available(),
    }