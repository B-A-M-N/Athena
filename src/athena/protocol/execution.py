"""Execution protocol.

All process execution initiated by the agent MUST flow through ExecutionManager
(INV-005). Application modules MUST NOT casually call subprocess.run/os.system
outside the execution subsystem. Streaming is the canonical API.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, AsyncIterator, Mapping, Protocol

from athena.protocol.tasks import NetworkPolicy


class RuntimePersistence(str, enum.Enum):
    PERSISTENT = "persistent"
    EPHEMERAL = "ephemeral"


@dataclass(frozen=True)
class ExecutionLimits:
    max_memory_mb: int | None = None
    max_cpu_seconds: int | None = None
    max_processes: int | None = None


@dataclass(frozen=True)
class ExecutionRequest:
    runtime: str
    source: str
    task_id: str
    workspace_id: str
    backend: str = "local"
    runtime_session_id: str | None = None
    persistence: RuntimePersistence = RuntimePersistence.EPHEMERAL
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    stdin: bytes | None = None
    timeout: timedelta | None = None
    network_policy: NetworkPolicy | None = None
    resource_limits: ExecutionLimits | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class ExecutionExitStatus(str, enum.Enum):
    EXITED = "exited"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class ExecutionEventType(str, enum.Enum):
    STARTED = "started"
    STDOUT = "stdout"
    STDERR = "stderr"
    DISPLAY = "display"
    ARTIFACT = "artifact"
    PROCESS_SPAWNED = "process_spawned"
    USAGE = "usage"
    EXITED = "exited"


@dataclass(frozen=True)
class ExecutionEvent:
    type: ExecutionEventType
    execution_id: str
    data: str | None = None
    exit_status: ExecutionExitStatus | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionResult:
    execution_id: str
    exit_code: int | None
    status: ExecutionExitStatus
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0


class Runtime(Protocol):
    async def create_session(
        self,
        *,
        task_id: str,
        backend: str,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> str:
        ...

    def execute(
        self,
        request: ExecutionRequest,
        execution_id: str,
    ) -> AsyncIterator[ExecutionEvent]:
        ...

    async def interrupt(self, execution_id: str) -> None:
        ...

    async def reset(self, runtime_session_id: str) -> None:
        ...

    async def close(self, runtime_session_id: str) -> None:
        ...


class ExecutionBackend(Protocol):
    name: str

    async def create_session(self, *, task_id: str, runtime: str, cwd: str | None, env: Mapping[str, str] | None) -> str:
        ...

    def execute(self, request: ExecutionRequest) -> AsyncIterator[ExecutionEvent]:
        ...

    async def interrupt(self, execution_id: str) -> None:
        ...

    async def destroy_session(self, runtime_session_id: str) -> None:
        ...


__all__ = [
    "RuntimePersistence", "ExecutionLimits", "ExecutionRequest",
    "ExecutionExitStatus", "ExecutionEventType", "ExecutionEvent",
    "ExecutionResult", "Runtime", "ExecutionBackend",
]