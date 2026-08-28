"""Deterministic affordance utility scoring; the kernel remains the decider."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class AffordanceOptimizer:
    """Rank existing machinery using proof, cost, reuse, and availability."""

    def score(
        self,
        *,
        lexical: float,
        descriptor: Any,
        record: Any = None,
        dependency_available: bool = True,
        environment_compatible: bool = True,
    ) -> tuple[float, dict[str, Any]]:
        proof = getattr(record, "proof_record", {}) if record is not None else {}
        successes = int(getattr(record, "success_count", 0) or 0)
        uses = int(getattr(record, "use_count", 0) or 0)
        quality = min(1.0, max(0.0, float(getattr(record, "quality_score", 0.0) or 0.0)))
        distinct = min(1.0, float(proof.get("distinct_inputs", 0) or 0) / 6.0)
        success_rate = successes / uses if uses else 0.0
        metric_provenance = proof.get("metric_provenance")
        event_metrics = metric_provenance if isinstance(metric_provenance, Mapping) else {}
        reuse_count = 0
        downstream_verifications = 0
        if event_metrics.get("reuse_count") == "canonical_generated_invocation":
            reuse_count = int(proof.get("reuse_count", 0) or 0)
        if event_metrics.get("downstream_verifications") == "canonical_passing_verification":
            downstream_verifications = int(proof.get("downstream_verifications", 0) or 0)
        reuse_bonus = min(0.5, reuse_count / 8.0)
        verification_bonus = min(0.75, downstream_verifications / 4.0)
        authority_cost = len(getattr(descriptor, "effects", ()) or ())
        cache_bonus = (
            0.25
            if str(getattr(getattr(descriptor, "cache_policy", None), "value", "")) == "ttl"
            else 0.0
        )
        availability_bonus = 0.5 if dependency_available and environment_compatible else -1.0
        score = (
            lexical
            + quality
            + success_rate
            + distinct
            + cache_bonus
            + reuse_bonus
            + verification_bonus
            + availability_bonus
            - (0.08 * authority_cost)
        )
        metrics = {
            "lexical": lexical,
            "verified_quality": quality,
            "success_rate": round(success_rate, 4),
            "reuse_potential": round(distinct, 4),
            "verified_reuse_count": reuse_count,
            "downstream_verifications": downstream_verifications,
            "reuse_bonus": round(reuse_bonus, 4),
            "verification_bonus": round(verification_bonus, 4),
            "authority_cost": authority_cost,
            "cache_bonus": cache_bonus,
            "dependency_available": dependency_available,
            "environment_compatible": environment_compatible,
        }
        return score, metrics


__all__ = ["AffordanceOptimizer"]
