"""CapabilityRegistry.

The single entry point for EVERY model-requested action (INV-004 / BHV-039).
Capabilities register under a CanonicalCapabilityDescriptor; requests are
resolved by id and their arguments schema-validated here, BEFORE any policy
evaluation (BHV-040). Only valid, resolved requests ever reach the policy
engine and executor.
"""

from __future__ import annotations

from typing import Any, Mapping

from athena.protocol.capabilities import (
    Availability,
    CapabilityDescriptor,
    CapabilityExecutor,
)
from athena.protocol.errors import CapabilityUnavailable, CapabilityValidationError
from athena.schema import compile_validator, format_schema_errors, validate_schema


class CapabilityRegistry:
    """Maps capability ids to executors and enforces schema validation."""

    def __init__(self) -> None:
        self._by_id: dict[str, CapabilityExecutor] = {}
        self._validators: dict[str, Any] = {}
        self._registration_audit: list[dict[str, Any]] = []
        self._generation = 0

    @property
    def generation(self) -> int:
        """Monotonic revision of the native capability inventory."""
        return self._generation

    def register(
        self, executor: CapabilityExecutor, *, authority: str = "native", replace: bool = False
    ) -> dict[str, Any]:
        """Register an executor by its descriptor id.

        Duplicate ids are a HARD error: later extensions (MCP, plugins,
        synthesized capabilities) must never silently shadow a native
        executor. Explicit replacement requires ``replace=True`` plus an
        ``authority`` label and is audited via the returned audit dict.
        """
        descriptor = getattr(executor, "descriptor", None)
        if descriptor is None or not isinstance(descriptor, CapabilityDescriptor):
            raise TypeError("executor must define a CapabilityDescriptor")
        validator = compile_validator(descriptor.input_schema)
        if descriptor.id in self._by_id and not replace:
            raise ValueError(
                f"capability '{descriptor.id}' already registered "
                f"(authority={authority}); use replace=True to override"
            )
        if descriptor.id in self._by_id and replace and not authority:
            raise ValueError("explicit capability replacement requires authority")
        audit = {
            "capability_id": descriptor.id,
            "replaced": self._by_id.get(descriptor.id).__class__.__name__
            if descriptor.id in self._by_id
            else None,
            "new_executor": executor.__class__.__name__,
            "authority": authority,
        }
        self._by_id[descriptor.id] = executor
        self._validators[descriptor.id] = validator
        self._registration_audit.append(audit)
        self._generation += 1
        return audit

    def replace(self, executor: CapabilityExecutor, *, authority: str) -> dict[str, Any]:
        """Explicitly replace an executor and return an audit record."""
        if not authority:
            raise ValueError("capability replacement requires authority")
        return self.register(executor, authority=authority, replace=True)

    def unregister(self, capability_id: str) -> None:
        if capability_id in self._by_id:
            del self._by_id[capability_id]
            self._validators.pop(capability_id, None)
            self._generation += 1

    def resolve(self, capability_id: str) -> CapabilityDescriptor:
        """Return the descriptor for a registered capability id."""
        executor = self._by_id.get(capability_id)
        if executor is None:
            raise CapabilityUnavailable(f"unknown capability: {capability_id}")
        return executor.descriptor

    def executor_for(self, capability_id: str) -> CapabilityExecutor:
        executor = self._by_id.get(capability_id)
        if executor is None:
            raise CapabilityUnavailable(f"unknown capability: {capability_id}")
        if executor.descriptor.availability is not Availability.AVAILABLE:
            raise CapabilityUnavailable(
                f"capability '{capability_id}' is {executor.descriptor.availability.value}"
            )
        return executor

    def iter_executors(self) -> tuple[CapabilityExecutor, ...]:
        """Return the canonical executor inventory without exposing storage."""
        return tuple(self._by_id.values())

    def validate(
        self,
        capability_id: str,
        arguments: Mapping[str, Any],
    ) -> None:
        """Validate arguments against the descriptor's input schema (BHV-040)."""
        executor = self._by_id.get(capability_id)
        if executor is None:
            raise CapabilityUnavailable(f"unknown capability: {capability_id}")
        descriptor = executor.descriptor
        for error in self._validate_descriptor(descriptor, arguments):
            raise CapabilityValidationError(f"invalid arguments for {capability_id}: {error}")

    def list_available(
        self,
        *,
        availability: Availability | None = None,
    ) -> list[CapabilityDescriptor]:
        """List registered descriptors, optionally filtered by availability."""
        out = []
        for executor in self._by_id.values():
            desc = executor.descriptor
            if availability is not None and desc.availability is not availability:
                continue
            out.append(desc)
        return sorted(out, key=lambda d: d.id)

    def list_descriptors(self) -> list[CapabilityDescriptor]:
        """Return only descriptors that can actually be invoked.

        ``list_available()`` keeps its historical unfiltered behavior for
        callers that need an inventory, while prompt/API consumers use this
        canonical usable-capability view.
        """
        return self.list_available(availability=Availability.AVAILABLE)

    def _validate_descriptor(
        self, descriptor: CapabilityDescriptor, arguments: Mapping[str, Any]
    ) -> list[str]:
        validator = self._validators.get(descriptor.id)
        if validator is None:
            validator = compile_validator(descriptor.input_schema)
            self._validators[descriptor.id] = validator
        instance = arguments if isinstance(arguments, dict) else arguments
        return format_schema_errors(validator.iter_errors(instance))


__all__ = ["CapabilityRegistry", "validate_schema"]
