"""Preference, reliability, latency, and cost ordering for admitted models."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from athena.models.inventory import EffectiveModelDescriptor
from athena.protocol.models import ModelInfo, PrivacyClass

_PRIVACY_RANK: dict[PrivacyClass, int] = {
    PrivacyClass.LOCAL: 0,
    PrivacyClass.UNKNOWN: 1,
    PrivacyClass.HYBRID: 2,
    PrivacyClass.REMOTE: 3,
}
_LATENCY_CLASS_RANK = {"fast": 0, "medium": 1, "slow": 2}
_ROUTING_PREFERENCES = frozenset({"balanced", "latency", "cost"})

__all__ = ["ModelRanking"]


class ModelRanking:
    """Choose one admitted descriptor using stable operational preferences."""

    def choose(
        self,
        candidates: Sequence[EffectiveModelDescriptor],
        *,
        stats: Mapping[tuple[str, str, str], tuple[int, int, float]],
        role: str,
        routing_preference: str = "balanced",
    ) -> EffectiveModelDescriptor:
        return min(
            candidates,
            key=lambda descriptor: self.selection_key(
                descriptor.info,
                stats,
                role,
                routing_preference,
            ),
        )

    @staticmethod
    def selection_key(
        info: ModelInfo,
        stats: Mapping[tuple[str, str, str], tuple[int, int, float]],
        role: str,
        routing_preference: str = "balanced",
    ) -> tuple:
        attempts, successes, total_latency = stats.get((info.provider, info.id, role), (0, 0, 0.0))
        reliability_penalty = (attempts - successes + 1) / (attempts + 4) if attempts else 0.25
        latency_observed = 0 if attempts else 1
        latency_value = total_latency / attempts if attempts else _declared_latency_rank(info)
        preference = str(routing_preference or "balanced").casefold()
        if preference not in _ROUTING_PREFERENCES:
            preference = "balanced"
        if preference == "cost":
            operational: tuple[Any, ...] = (
                *_cost_sort_key(info),
                latency_observed,
                latency_value,
            )
        else:
            operational = (
                latency_observed,
                latency_value,
                *_cost_sort_key(info),
            )
        return (
            _PRIVACY_RANK.get(info.privacy_class, _PRIVACY_RANK[PrivacyClass.UNKNOWN]),
            reliability_penalty,
            *operational,
            f"{info.provider}/{info.id}",
        )

    @staticmethod
    def rationale(
        best: ModelInfo,
        *,
        policy: Any,
        requirements: Any,
        history_used: bool = False,
    ) -> tuple[str, ...]:
        parts = [f"model={best.id}", f"provider={best.provider}"]
        if policy.privacy:
            parts.append(f"privacy={best.privacy_class.value}")
        floor = getattr(policy, "min_quality_tier", None)
        if floor:
            parts.append(f"quality_floor={floor}")
        if requirements.required_capabilities:
            parts.append("caps=" + ",".join(sorted(requirements.required_capabilities)))
        if history_used:
            parts.append("history=rolling_attempts")
        parts.append(f"preference={policy.routing_preference}")
        return tuple(parts)


def _cost_sort_key(info: ModelInfo) -> tuple[int, float]:
    cost = info.cost
    if (
        cost is None
        or cost.currency.upper() != "USD"
        or cost.per_1m_input is None
        or cost.per_1m_output is None
    ):
        return (2, float("inf"))
    total = float(cost.per_1m_input) + float(cost.per_1m_output)
    return (0 if total == 0 else 1, total)


def _declared_latency_rank(info: ModelInfo) -> int:
    return _LATENCY_CLASS_RANK.get(str(info.latency_class or "medium").casefold(), 1)
