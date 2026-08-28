"""Capability protocol.

Every model-requested external action MUST pass through:
CapabilityRegistry -> PolicyEngine -> Capability executor (INV-004).
Arguments MUST be schema-validated before policy evaluation.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from athena.protocol.tasks import (
    AutonomyLevel,
    CapabilityPolicy,
    ResourceBudget,
    WorkspaceSpec,
)


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
    GENERATED = "generated"
    REMOTE = "remote"


@dataclass(frozen=True)
class DispatchDirectives:
    """Internal controls supplied by trusted orchestration only.

    These values deliberately do not live on ``CapabilityRequest``.  A model
    request may describe an operation, but it must not be able to assert the
    revision it is authorized to mutate.  The dispatcher attaches directives
    to the invocation context after policy and routing have selected the
    executor.
    """

    expected_preimages: Mapping[str, str] = field(default_factory=dict)
    expected_modes: Mapping[str, int] = field(default_factory=dict)
    transaction_id: str | None = None
    reality_tier: str | None = None
    # Workflow execution identity is internal provenance. It binds a
    # suspended capability continuation back to the exact durable step that
    # created it without making the model-visible request an authority field.
    workflow_run_id: str | None = None
    workflow_step_id: str | None = None
    workflow_item_index: int | None = None
    workflow_execution_id: str | None = None
    # A workflow call may be suspended on one of its child steps.  These
    # orchestration fields let the child approval finish the exact outer
    # workflow call after approval, including after a restart.
    workflow_parent_call_id: str | None = None
    workflow_parent_capability_id: str | None = None
    workflow_id: str | None = None
    # Authority inherited from an already-authorized generated parent call.
    # These are dispatcher-created controls; model input cannot set them.
    inherited_effects: frozenset[EffectClass] = frozenset()
    inherited_capability_id: str | None = None


class Availability(str, enum.Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


class CachePolicy(str, enum.Enum):
    """Explicit result-cache contract for a capability descriptor."""

    NONE = "none"
    TTL = "ttl"
    WORKSPACE_REVISION = "workspace_revision"
    CONTENT_ADDRESS = "content_address"


class ExternalEffectPhase(str, enum.Enum):
    """Lifecycle phases for effects that cannot be shadowed locally."""

    INSPECT = "inspect"
    PREPARE = "prepare"
    DRY_RUN = "dry_run"
    APPLY = "apply"
    VERIFY = "verify"
    COMPENSATE = "compensate"


@dataclass(frozen=True)
class ExternalEffectContract:
    """Declared lifecycle and proof contract for an external effect.

    This describes a capability's protocol; it does not grant authority. The
    dispatcher still evaluates the concrete effect and policy for every call.
    """

    phases: frozenset[ExternalEffectPhase] = frozenset()
    idempotency_required: bool = True
    reversible: bool = False
    compensatable: bool = False
    approval_floor: str = "ask"
    identity_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        normalized = frozenset(
            phase if isinstance(phase, ExternalEffectPhase) else ExternalEffectPhase(phase)
            for phase in self.phases
        )
        object.__setattr__(self, "phases", normalized)
        if self.approval_floor not in {"ask", "deny"}:
            raise ValueError("external effect approval_floor must be ask or deny")
        if ExternalEffectPhase.APPLY in normalized and not normalized & {
            ExternalEffectPhase.PREPARE,
            ExternalEffectPhase.DRY_RUN,
        }:
            raise ValueError("external apply contract requires prepare or dry_run support")
        if self.reversible and ExternalEffectPhase.COMPENSATE not in normalized:
            raise ValueError("reversible external effect contract requires compensate support")
        if self.compensatable and ExternalEffectPhase.COMPENSATE not in normalized:
            raise ValueError("compensatable external effect contract requires compensate support")

    def to_record(self) -> dict[str, Any]:
        return {
            "phases": sorted(phase.value for phase in self.phases),
            "idempotency_required": self.idempotency_required,
            "reversible": self.reversible,
            "compensatable": self.compensatable,
            "approval_floor": self.approval_floor,
            "identity_fields": list(self.identity_fields),
        }


@dataclass(frozen=True)
class ExternalEffectReceipt:
    """Durable identity and outcome for one external-effect transaction."""

    receipt_id: str
    transaction_id: str
    capability_id: str
    phase: ExternalEffectPhase
    status: str
    external_identity: str
    request_digest: str
    idempotency_key: str | None = None
    response: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "transaction_id": self.transaction_id,
            "capability_id": self.capability_id,
            "phase": self.phase.value,
            "status": self.status,
            "external_identity": self.external_identity,
            "request_digest": self.request_digest,
            "idempotency_key": self.idempotency_key,
            "response": dict(self.response),
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


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
    cache_policy: CachePolicy = CachePolicy.NONE
    cache_ttl_seconds: float | None = None
    operation_cache_policies: Mapping[str, CachePolicy] | None = None
    cache_key_resolver: Callable[[Mapping[str, Any], WorkspaceSpec], str | None] | None = None
    external_effects: Mapping[str, ExternalEffectContract] | None = None

    def __post_init__(self) -> None:
        """Attach the native operation contract at descriptor creation.

        The compatibility map is only a migration source for the existing
        native descriptors; dispatch consumes the immutable contract stored on
        this descriptor. Third-party descriptors must provide their own map or
        remain simple, non-operation capabilities.
        """
        if isinstance(self.cache_policy, str):
            object.__setattr__(self, "cache_policy", CachePolicy(self.cache_policy))
        if self.operation_cache_policies is not None:
            object.__setattr__(
                self,
                "operation_cache_policies",
                {
                    str(operation): (
                        policy if isinstance(policy, CachePolicy) else CachePolicy(policy)
                    )
                    for operation, policy in self.operation_cache_policies.items()
                },
            )
        if self.external_effects is not None:
            object.__setattr__(
                self,
                "external_effects",
                {
                    str(operation): (
                        contract
                        if isinstance(contract, ExternalEffectContract)
                        else ExternalEffectContract(**dict(contract))
                    )
                    for operation, contract in self.external_effects.items()
                },
            )
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

    def resolve_cache_policy(self, arguments: Mapping[str, Any]) -> CachePolicy:
        """Resolve cache semantics for an operation without guessing from effects."""
        if self.operation_cache_policies is None:
            return self.cache_policy
        operation = str(arguments.get("operation") or arguments.get("action") or "").lower()
        return self.operation_cache_policies.get(operation, CachePolicy.NONE)

    def resolve_external_effect_contract(
        self,
        arguments: Mapping[str, Any],
    ) -> ExternalEffectContract | None:
        """Return the contract for the concrete operation, if declared."""
        if self.external_effects is None:
            return None
        operation = str(arguments.get("operation") or arguments.get("action") or "").lower()
        return self.external_effects.get(operation)

    def resolve_external_identity(
        self,
        arguments: Mapping[str, Any],
        workspace: WorkspaceSpec | None = None,
    ) -> str | None:
        """Derive the durable identity of a governed external transaction.

        ``external_identity`` may remain in a compatibility input schema as a
        caller-visible label, but it is never used for receipt authority or
        idempotency.  Identity is derived from the concrete target and the
        operation contract instead.
        """
        contract = self.resolve_external_effect_contract(arguments)
        if contract is None:
            return None
        operation = str(arguments.get("operation") or arguments.get("action") or "").lower()
        if self.id == "network" and operation == "http_transaction":
            return _canonical_http_identity(arguments)
        if self.id == "service" and operation == "service_transaction":
            unit = str(arguments.get("unit") or "").strip()
            service_operation = str(arguments.get("service_operation") or "").lower()
            scope = "user" if bool(arguments.get("user_scope")) else "system"
            return f"systemd:{scope}:{unit}:{service_operation}"
        if self.id == "database" and operation == "database_transaction":
            raw_path = str(arguments.get("path") or "")
            if workspace is not None and workspace.root and not os.path.isabs(raw_path):
                raw_path = os.path.join(workspace.root, raw_path)
            path = os.path.realpath(raw_path)
            sql_digest = hashlib.sha256(
                str(arguments.get("sql") or "").strip().encode()
            ).hexdigest()
            return f"sqlite:{path}:{sql_digest}"

        # Custom contracts remain deterministic without allowing callers to
        # select the durable key. Field order is contract-defined and values
        # are serialized canonically.
        values = {
            field: _canonical_identity_value(field, arguments.get(field))
            for field in contract.identity_fields
        }
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":"))
        return f"{self.id}:{operation}:{encoded}"

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
        record["cache_policy"] = self.cache_policy.value
        if self.cache_ttl_seconds is not None:
            record["cache_ttl_seconds"] = self.cache_ttl_seconds
        if self.operation_cache_policies is not None:
            record["operation_cache_policies"] = {
                operation: policy.value
                for operation, policy in self.operation_cache_policies.items()
            }
        if self.cache_key_resolver is not None:
            record["dynamic_cache_key"] = True
        if self.external_effects is not None:
            record["external_effects"] = {
                operation: contract.to_record()
                for operation, contract in self.external_effects.items()
            }
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
    capability_policy: CapabilityPolicy | None = None
    resource_budget: ResourceBudget | None = None
    # Internal execution context only. This deliberately does not belong on
    # CapabilityRequest, where model-visible fields could be mistaken for
    # authority controls.
    autonomy: AutonomyLevel | str | None = None
    # Generated-tool composition is an internal execution chain, not model
    # input.  The dispatcher propagates it only across mediated host calls so
    # nested generated tools cannot recurse forever or hide a cycle.
    generated_call_depth: int = 0
    generated_call_chain: tuple[str, ...] = ()
    directives: DispatchDirectives = field(default_factory=DispatchDirectives)


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
    ) -> CapabilityResult: ...


class CapabilityOutputSink(Protocol):
    async def chunk(self, text: str, *, stream: str = "stdout") -> None: ...


class Capability(Protocol):
    descriptor: CapabilityDescriptor

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        *,
        task: Any,
        ctx: Any,
    ) -> CapabilityResult: ...


def _canonical_http_identity(arguments: Mapping[str, Any]) -> str:
    method = str(arguments.get("method") or "GET").upper()
    parsed = urlsplit(str(arguments.get("url") or ""))
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        port = None
    default_port = (parsed.scheme.casefold() == "http" and port == 80) or (
        parsed.scheme.casefold() == "https" and port == 443
    )
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if port is not None and not default_port:
        netloc = f"{netloc}:{port}"
    normalized_url = urlunsplit(
        (
            parsed.scheme.casefold(),
            netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )
    return f"{method} {normalized_url}"


def _canonical_identity_value(field: str, value: Any) -> Any:
    if field.endswith("path") or field == "path":
        return os.path.realpath(str(value or ""))
    if isinstance(value, str):
        return value.strip()
    return value


__all__ = [
    "Availability",
    "CachePolicy",
    "Capability",
    "CapabilityDescriptor",
    "CapabilityExecutor",
    "ExternalEffectContract",
    "ExternalEffectPhase",
    "ExternalEffectReceipt",
    "CapabilityOrigin",
    "CapabilityOutputSink",
    "CapabilityRequest",
    "CapabilityRequestOrigin",
    "CapabilityResult",
    "CapabilityResultStatus",
    "EffectClass",
    "InvocationContext",
    "DispatchDirectives",
]
