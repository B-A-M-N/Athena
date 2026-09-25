"""Provider-neutral response-shape comparison records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

__all__ = ["ProviderParityObservation", "compare_provider_parity"]


@dataclass(frozen=True)
class ProviderParityObservation:
    """One normalized provider response, excluding wire-specific details."""

    provider: str
    model: str
    block_kinds: tuple[str, ...]
    text: str
    tool_calls: tuple[tuple[str, str], ...]
    input_tokens: int = 0
    output_tokens: int = 0

    def signature(self) -> tuple[Any, ...]:
        return (
            self.block_kinds,
            self.text,
            self.tool_calls,
            int(self.input_tokens),
            int(self.output_tokens),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "block_kinds": list(self.block_kinds),
            "text": self.text,
            "tool_calls": [list(item) for item in self.tool_calls],
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


def compare_provider_parity(
    case_id: str,
    observations: Mapping[str, ProviderParityObservation],
) -> dict[str, Any]:
    """Compare adapter-normalized observations on one fixed request."""
    if not case_id or not observations:
        raise ValueError("provider parity requires a case id and observations")
    signatures = {name: observation.signature() for name, observation in observations.items()}
    reference_name = sorted(signatures)[0]
    reference = signatures[reference_name]
    mismatches = {
        name: {"expected": repr(reference), "observed": repr(signature)}
        for name, signature in signatures.items()
        if signature != reference
    }
    return {
        "schema": "athena.evaluation.provider-parity.v1",
        "case_id": case_id,
        "reference_provider": reference_name,
        "providers": sorted(observations),
        "shape_parity": not mismatches,
        "mismatches": mismatches,
        "observations": {
            name: observation.to_record() for name, observation in sorted(observations.items())
        },
        "limitations": (
            "Adapter fixture parity only; no live external provider, credential, "
            "rate-limit, or model-quality claim is made."
        ),
    }
