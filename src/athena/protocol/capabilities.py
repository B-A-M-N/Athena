"""Capability protocol.

Every model-requested external action MUST pass through:
CapabilityRegistry -> PolicyEngine -> Capability executor (INV-004).
Arguments MUST be schema-validated before policy evaluation.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from athena.protocol.tasks import WorkspaceSpec


class EffectClass(str, enum.Enum):
    READ_LOCAL = "READ_LOCAL"
    WRITE_LOCAL = "WRITE_LOCAL"
    EXECUTE = "EXECUTE"
    SPAWN_PROCESS = "SPAWN_PROCESS"
    NETWORK_READ = "NETWORK_READ"
    NETWORK_WRITE = "NETWORK_WRITE"
    SECRET_READ = "SECRET_READ"
    DELETE = "DELETE"
    PRIVILEGED = "PRIVILEGED"
    EXTERNAL_MESSAGE = "EXTERNAL_MESSAGE"
    EXTERNAL_PUBLISH = "EXTERNAL_PUBLISH"
    COMPUTER_INPUT = "COMPUTER_INPUT"
    FINANCIAL = "FINANCIAL"


class CapabilityOrigin(str, enum.Enum):
    NATIVE = "native"
    MCP = "MCP"
    PLUGIN = "plugin"
    PROJECT = "project"
    REMOTE = "remote"


class Availability(str, enum.Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


@dataclass(frozen=True)
class CapabilityDescriptor:
    id: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    effects: frozenset[EffectClass] = frozenset()
    tags: frozenset[str] = frozenset()
    origin: CapabilityOrigin = CapabilityOrigin.NATIVE
    version: str = "1"
    availability: Availability = Availability.AVAILABLE


@dataclass(frozen=True)
class CapabilityRequest:
    capability_id: str
    arguments: Mapping[str, Any]
    task_id: str
    session_id: str | None = None
    call_id: str = ""


@dataclass(frozen=True)
class InvocationContext:
    """Per-invocation execution scope for a capability call.

    Carries the CURRENT task's workspace so executors resolve paths (fs) and
    cwd (execute) against the task workspace, not a constructor-bound root
    (P0-8). Passed by the dispatcher to ``CapabilityExecutor.invoke``.
    """

    workspace: WorkspaceSpec
    task_id: str | None = None
    credentials: Mapping[str, Any] = field(default_factory=dict)
    execution_backend: str = "local"


class CapabilityResultStatus(str, enum.Enum):
    OK = "ok"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class CapabilityResult:
    call_id: str
    capability_id: str
    status: CapabilityResultStatus
    output: str = ""
    error: str | None = None
    ref_uri: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class CapabilityExecutor(Protocol):
    descriptor: CapabilityDescriptor

    async def invoke(
        self,
        request: CapabilityRequest,
        *,
        output_accumulator: "CapabilityOutputSink | None" = None,
        context: "InvocationContext | None" = None,
    ) -> CapabilityResult:
        ...


class CapabilityOutputSink(Protocol):
    async def chunk(self, text: str, *, stream: str = "stdout") -> None:
        ...


class Capability(Protocol):
    descriptor: CapabilityDescriptor

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        *,
        task: Any,
        ctx: Any,
    ) -> CapabilityResult:
        ...


__all__ = [
    "EffectClass", "CapabilityOrigin", "Availability", "CapabilityDescriptor",
    "Capability", "CapabilityRequest", "InvocationContext", "CapabilityResult",
    "CapabilityResultStatus", "CapabilityExecutor", "CapabilityOutputSink",
]