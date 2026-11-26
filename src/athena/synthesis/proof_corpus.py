"""Schema/effect-derived proof cases for generated capabilities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DerivedProofCase:
    id: str
    kind: str
    input: Any
    expected: str
    source: str = "schema_and_effects"
    analyzer_status: str = "not_run"

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "input": self.input,
            "expected": self.expected,
            "verification_claim": f"{self.kind}:{self.expected}",
            "source": self.source,
            "analyzer_status": self.analyzer_status,
        }


def derive_proof_cases(
    schema: Mapping[str, Any],
    effects: Mapping[str, Any] | Iterable[str] = (),
) -> tuple[DerivedProofCase, ...]:
    """Derive positive/negative cases from the admitted schema and effects."""
    if not isinstance(schema, Mapping):
        raise TypeError("generated capability schema must be an object")
    effect_values = (
        set(str(key) for key in effects)
        if not isinstance(effects, Mapping)
        else {str(key) for key, value in effects.items() if value}
    )
    schema_type = str(schema.get("type") or "object")
    valid: Any = {} if schema_type == "object" else [] if schema_type == "array" else "probe"
    if schema_type == "object":
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            for name, definition in properties.items():
                if isinstance(definition, Mapping) and "default" in definition:
                    valid[str(name)] = definition["default"]
        required = schema.get("required")
        if isinstance(required, list):
            for name in required:
                definition = properties.get(name) if isinstance(properties, Mapping) else None
                valid.setdefault(str(name), _schema_example(definition))
    cases: list[DerivedProofCase] = [
        _case("valid", "positive", valid, "accept"),
        _case("wrong-root", "negative", None, "reject"),
    ]
    if schema_type == "object" and schema.get("additionalProperties") is False:
        cases.append(_case("unknown-field", "negative", {"__athena_unknown__": True}, "reject"))
    if schema.get("required"):
        cases.append(_case("missing-required", "negative", {}, "reject"))
    cases.append(
        _case(
            "effect-ceiling",
            "effect",
            sorted(effect_values),
            "reject" if effect_values - {"READ_LOCAL", "EXECUTE"} else "analyze",
        )
    )
    return tuple(cases)


def corpus_digest(cases: tuple[DerivedProofCase, ...] | list[DerivedProofCase]) -> str:
    payload = [case.to_record() for case in cases]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _schema_example(schema: Any) -> Any:
    if not isinstance(schema, Mapping):
        return None
    if "default" in schema:
        return schema["default"]
    kind = schema.get("type")
    if kind == "string":
        return "proof"
    if kind in {"integer", "number"}:
        return 0
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    if kind == "object":
        return {}
    return None


def _case(name: str, kind: str, value: Any, expected: str) -> DerivedProofCase:
    digest = hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()[:12]
    return DerivedProofCase(
        id=f"derived:{name}:{digest}",
        kind=kind,
        input=value,
        expected=expected,
    )


__all__ = ["DerivedProofCase", "corpus_digest", "derive_proof_cases"]
