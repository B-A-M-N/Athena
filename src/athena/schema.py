"""Neutral JSON Schema compilation and validation utilities."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["compile_validator", "format_schema_errors", "validate_schema"]


def validate_schema(schema: Mapping[str, Any], arguments: Any) -> list[str]:
    """Validate values against the version-pinned JSON Schema implementation.

    This is shared by capabilities, workflows, affordances, and synthesis so
    schema admission has one implementation and one depth limit.
    """
    validator = compile_validator(schema)
    return format_schema_errors(validator.iter_errors(arguments))


def compile_validator(schema: Mapping[str, Any]):
    """Compile and validate one bounded JSON Schema document."""
    try:
        from jsonschema.validators import validator_for  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - dependency is mandatory
        raise RuntimeError("jsonschema is required for JSON schemas") from exc
    effective = _effective_schema(schema)
    if _schema_depth(effective) > _MAX_SCHEMA_DEPTH:
        raise ValueError(f"JSON Schema exceeds maximum nesting depth {_MAX_SCHEMA_DEPTH}")
    validator_cls = validator_for(effective)
    validator_cls.check_schema(effective)
    return validator_cls(effective)


def format_schema_errors(errors) -> list[str]:
    """Format validator errors deterministically for protocol-facing callers."""
    formatted: list[str] = []
    for err in sorted(errors, key=lambda e: list(e.absolute_path)):
        path = "/".join(str(p) for p in err.absolute_path) or "(root)"
        formatted.append(f"{path}: {err.message}")
    return formatted


def _effective_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Translate Athena's legacy alias without weakening JSON Schema."""
    effective = dict(schema)
    if effective.pop("allow_extra", True) is False and "additionalProperties" not in effective:
        effective["additionalProperties"] = False
    if "$schema" not in effective:
        effective["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return effective


# Maximum nesting depth for a JSON Schema document. Legitimate tool schemas
# are a handful of levels deep; unbounded nesting lets an attacker-supplied
# schema drive validation into RecursionError instead of failing admission.
_MAX_SCHEMA_DEPTH = 32


def _schema_depth(node: Any, seen: int = 0) -> int:
    if seen > _MAX_SCHEMA_DEPTH:
        return seen
    if isinstance(node, Mapping):
        return max(
            (_schema_depth(value, seen + 1) for value in node.values()),
            default=seen,
        )
    if isinstance(node, (list, tuple)):
        return max(
            (_schema_depth(item, seen + 1) for item in node),
            default=seen,
        )
    return seen
