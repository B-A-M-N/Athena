"""Capability protocol.

Every model-requested external action MUST pass through:
CapabilityRegistry -> PolicyEngine -> Capability executor (INV-004).
Arguments MUST be schema-validated before policy evaluation.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

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
    GENERATED = "generated"
    MCP = "MCP"
    PLUGIN = "plugin"
    PROJECT = "project"
    REMOTE = "remote"


class CapabilityRequestOrigin(str, enum.Enum):
    """Trust/provenance of a capability request."""

    MODEL = "model"
    USER_DIRECT = "user_direct"
    TRUSTED_ORCHESTRATION = "trusted_orchestration"
    SYSTEM = "system"
    MCP = "mcp"


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
    operation_effects: Mapping[str, frozenset[EffectClass]] | None = None
    effect_resolver: Callable[[Mapping[str, Any]], frozenset[EffectClass]] | None = None

    def __post_init__(self) -> None:
        """Attach the native operation contract at descriptor creation.

        The compatibility map is only a migration source for the existing
        native descriptors; dispatch consumes the immutable contract stored on
        this descriptor. Third-party descriptors must provide their own map or
        remain simple, non-operation capabilities.
        """
        if self.operation_effects is not None or self.effect_resolver is not None:
            return
        try:
            from athena.capabilities.operations import OPERATION_EFFECTS

            mapping = OPERATION_EFFECTS.get(self.id)
        except ImportError:
            mapping = None
        if mapping is not None:
            object.__setattr__(self, "operation_effects", dict(mapping))

    def resolve_effects(self, arguments: Mapping[str, Any]) -> frozenset[EffectClass] | None:
        """Resolve exact operation effects, or ``None`` for simple contracts."""
        if self.effect_resolver is not None:
            effects = set(self.effect_resolver(arguments))
            outside = set(effects) - set(self.effects)
            if outside:
                raise ValueError(
                    "resolved effects exceed the descriptor envelope: "
                    + ", ".join(sorted(effect.value for effect in outside))
                )
            return frozenset(effects)
        if self.operation_effects is None:
            return None
        op = str(arguments.get("operation") or arguments.get("action") or "").lower()
        if op not in self.operation_effects:
            raise ValueError(f"operation {op!r} has no declared effect classification")
        effects = set(self.operation_effects[op])
        if self.id == "network" and op == "http":
            method = str(arguments.get("method") or "GET").upper()
            if method not in {"GET", "HEAD", "OPTIONS"}:
                effects.add(EffectClass.NETWORK_WRITE)
        outside = effects - set(self.effects)
        if outside:
            raise ValueError(
                f"operation {op!r} requires effects beyond the descriptor envelope: "
                + ", ".join(sorted(effect.value for effect in outside))
            )
        return frozenset(effects)

    def to_record(self) -> dict[str, Any]:
        """Return the model/API-safe descriptor representation.

        ``effect_resolver`` is executable policy, not serializable model
        metadata.  Generic ``__dict__`` serialization used to expose it as
        an empty object, which made a dynamic descriptor look like it had a
        broken operation contract.  Publish the static envelope and whether
        operation effects are resolved dynamically instead.
        """
        record: dict[str, Any] = {
            "id": self.id,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "effects": sorted(effect.value for effect in self.effects),
            "tags": sorted(self.tags),
            "origin": self.origin.value,
            "version": self.version,
            "availability": self.availability.value,
        }
        if self.operation_effects is not None:
            record["operation_effects"] = {
                operation: sorted(effect.value for effect in effects)
                for operation, effects in self.operation_effects.items()
            }
        if self.effect_resolver is not None:
            record["dynamic_effects"] = True
        return record


@dataclass(frozen=True)
class CapabilityRequest:
    capability_id: str
    arguments: Mapping[str, Any]
    task_id: str | None
    session_id: str | None = None
    call_id: str = ""
    origin: CapabilityRequestOrigin = CapabilityRequestOrigin.MODEL
    candidate: Any = None


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
        output_accumulator: CapabilityOutputSink | None = None,
        context: InvocationContext | None = None,
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
    "Availability", "Capability", "CapabilityDescriptor", "CapabilityExecutor",
    "CapabilityOrigin", "CapabilityOutputSink", "CapabilityRequest",
    "CapabilityRequestOrigin", "CapabilityResult", "CapabilityResultStatus",
    "EffectClass", "InvocationContext",
]
