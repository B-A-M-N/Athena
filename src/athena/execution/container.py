"""Container execution backend (BUILDSPEC 51, initial backends: local, container).

The type is kept importable while the backend is being completed.  Docker
reachability alone is not sufficient to advertise availability: the execution,
interrupt, session teardown, and shutdown contract must all be implemented.
"""

from __future__ import annotations

from typing import AsyncIterator, Mapping

from athena.execution.backend import ExecutionBackend
from athena.protocol.execution import ExecutionEvent, ExecutionRequest

__all__ = ["ContainerBackend"]

try:
    import docker  # type: ignore[import-untyped]
    _DOCKER_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    docker = None
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
        # Do not report a reachable daemon as a usable backend while the
        # execution contract below is incomplete.  A false availability result
        # is safer than advertising a backend that fails after selection.
        return False

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
        workspace_root: str | None = None,
        network_policy: str | None = None,
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
