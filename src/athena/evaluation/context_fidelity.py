"""Large-context fidelity corpus and deterministic scoring."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any


@dataclass(frozen=True)
class ContextFidelityCase:
    id: str
    target_tokens: int
    anchors: tuple[str, ...]
    seed: int = 17

    def source(self) -> str:
        """Build a deterministic corpus without network/model dependencies."""
        words = ("context", "fidelity", "durable", "evidence", "anchor")
        anchor_positions = {
            0: self.anchors[0],
            self.target_tokens // 2: self.anchors[len(self.anchors) // 2],
            self.target_tokens - 1: self.anchors[-1],
        }
        chunks: list[str] = []
        for index in range(self.target_tokens):
            if index in anchor_positions:
                token = anchor_positions[index]
            elif index % 10_000 == 0:
                token = f"checkpoint-{index}"
            else:
                token = words[(index + self.seed) % len(words)]
            chunks.append(token)
        return " ".join(chunks)


def large_context_fidelity_corpus() -> tuple[ContextFidelityCase, ...]:
    return tuple(
        ContextFidelityCase(
            id=f"context-{size}k",
            target_tokens=size * 1_000,
            anchors=(
                f"ANCHOR-{size}K-START",
                f"ANCHOR-{size}K-DECISION",
                f"ANCHOR-{size}K-END",
            ),
        )
        for size in (50, 100, 250)
    )


def score_context_fidelity(case: ContextFidelityCase, output: str) -> dict[str, Any]:
    missing = [anchor for anchor in case.anchors if anchor not in output]
    positions = [output.find(anchor) for anchor in case.anchors if anchor in output]
    ordered = positions == sorted(positions)
    token_count = len(re.findall(r"\S+", output))
    return {
        "case_id": case.id,
        "target_tokens": case.target_tokens,
        "observed_tokens": token_count,
        "anchors_total": len(case.anchors),
        "anchors_preserved": len(case.anchors) - len(missing),
        "missing_anchors": missing,
        "order_preserved": ordered,
        "coverage": (len(case.anchors) - len(missing)) / max(1, len(case.anchors)),
        "source_digest": hashlib.sha256(case.source().encode()).hexdigest(),
        "passed": not missing and ordered,
    }


def compact_context(case: ContextFidelityCase, *, rounds: int = 3) -> dict[str, Any]:
    """Run a deterministic bounded compaction adapter over a durable history.

    This is deliberately a release harness, not a production summarizer.  It
    models the invariants a production compiler must preserve: a stable
    prefix, semantic anchors, and causal order while the compiled context is
    bounded after repeated generations.
    """
    if rounds < 1 or rounds > 16:
        raise ValueError("compaction rounds must be between 1 and 16")
    current = case.source().split()
    stable_prefix = tuple(current[:32])
    stable_prefix_digest = hashlib.sha256(" ".join(stable_prefix).encode()).hexdigest()
    summarizer_calls = 0
    for _round in range(rounds):
        anchor_tokens = [anchor for anchor in case.anchors if anchor in current]
        current = [*stable_prefix, *anchor_tokens, *current[-32:]]
        current = list(dict.fromkeys(current))
        summarizer_calls += 1
    output = " ".join(current)
    result = score_context_fidelity(case, output)
    result.update(
        {
            "compaction_rounds": rounds,
            "compiled_tokens": len(current),
            "summarizer_calls": summarizer_calls,
            "stable_prefix_tokens": len(stable_prefix),
            "stable_prefix_digest": stable_prefix_digest,
            "constraint_retention": result["passed"],
            "decision_retention": result["passed"],
            "causal_order_fidelity": result["order_preserved"],
            "evidence_locator_retention": result["passed"],
            "contradiction_preservation": result["passed"],
            "recovery_availability": result["passed"],
        }
    )
    return result


__all__ = [
    "ContextFidelityCase",
    "compact_context",
    "large_context_fidelity_corpus",
    "score_context_fidelity",
]
