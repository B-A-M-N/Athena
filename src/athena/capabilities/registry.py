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


class CapabilityRegistry:
    """Maps capability ids to executors and enforces schema validation."""

    def __init__(self) -> None:
        self._by_id: dict[str, CapabilityExecutor] = {}

    def register(self, executor: CapabilityExecutor) -> None:
        """Register an executor by its descriptor id."""
        descriptor = getattr(executor, "descriptor", None)
        if descriptor is None or not isinstance(descriptor, CapabilityDescriptor):
            raise TypeError("executor must define a CapabilityDescriptor")
        self._by_id[descriptor.id] = executor

    def unregister(self, capability_id: str) -> None:
        if capability_id in self._by_id:
            del self._by_id[capability_id]

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
        return executor

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
        for error in validate_schema(descriptor.input_schema, arguments):
            raise CapabilityValidationError(
                f"invalid arguments for {capability_id}: {error}"
            )

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
        """Alias for :meth:`list_available` (returns ``list[CapabilityDescriptor]``)."""
        return self.list_available()


def validate_schema(schema: dict[str, Any], arguments: Mapping[str, Any]) -> list[str]:
    """Lightweight JSON-schema subset validator used before policy evaluation.

    Returns a list of human-readable validation problems. This keeps the
    registry dependency-free while still enforcing required fields, unknown
    property rejection and enum/type checks (BHV-040).
    """
    errors: list[str] = []
    if not hasattr(arguments, "get") or not hasattr(arguments, "items"):
        return ["arguments must be an object"]

    allowed_keys = schema.get("allow_extra", True)
    properties = schema.get("properties") or {}
    if allowed_keys is False:
        extra = set(arguments) - set(properties)
        if extra:
            errors.append(f"unknown keys: {', '.join(sorted(extra))}")

    for prop, spec in properties.items():
        if prop not in arguments:
            continue
        value = arguments[prop]
        expected = spec.get("type")
        if expected == "string" and not isinstance(value, str):
            errors.append(f"{prop}: expected string")
        elif expected == "boolean" and not isinstance(value, bool):
            errors.append(f"{prop}: expected boolean")
        elif expected == "number" and not isinstance(value, (int, float)):
            errors.append(f"{prop}: expected number")
        enum = spec.get("enum")
        if enum is not None and value not in enum:
            errors.append(f"{prop}: must be one of {enum}")

    required = schema.get("required") or []
    for prop in required:
        if prop not in arguments:
            errors.append(f"missing required field: {prop}")
    return errors


__all__ = ["CapabilityRegistry", "validate_schema"]