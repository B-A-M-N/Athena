"""ModelRouter — policy-driven provider/model selection (BUILDSPEC 26-27, BHV-034..038).

The router orchestrates normalized inventory, hard admission, and deterministic
ranking; it holds NO provider-specific knowledge (INV-006). Capability
filtering (BHV-034), provider neutrality (BHV-035), privacy/offline discipline
(BHV-037/038), and cost remain explicit gates before ranking.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import time
from typing import Any, Protocol

from athena.models.admission import (
    CAP_AUDIO_INPUT,
    CAP_AUDIO_OUTPUT,
    CAP_REASONING,
    CAP_STREAMING,
    CAP_STRUCTURED,
    CAP_TOOLS,
    CAP_VISION,
    ModelAdmission,
)
from athena.models.inventory import EffectiveModelDescriptor
from athena.models.ranking import ModelRanking
from athena.protocol.errors import ModelUnavailable, ProviderUnavailable
from athena.protocol.models import ModelInfo, ModelQualityTier, ModelRequirements
from athena.protocol.tasks import ModelPolicy

_NO_MODELS = ("__athena_no_model_intersection__",)


@dataclass(frozen=True)
class ModelSelection:
    provider: str
    model: str
    info: ModelInfo
    rationale: tuple[str, ...] = ()
    effective: EffectiveModelDescriptor | None = None

    def __str__(self) -> str:
        return f"{self.provider}/{self.model}"


class ModelSource(Protocol):
    """Registry surface the router depends on."""

    async def list_models(self) -> Sequence[ModelInfo]: ...

    async def list_effective_models(self) -> Sequence[EffectiveModelDescriptor]: ...

    def provider_for(self, provider_name: str) -> object: ...

    def readiness(self) -> Mapping[str, object]: ...


class ModelRouter:
    """Deterministic policy-driven selection over registered models.

    ``role_policies`` maps a role name ("summarizer", "judge", "coder", ...)
    to a fallback :class:`ModelPolicy`. When a caller passes a policy whose
    ``allowed`` is empty and whose role has an entry, the role's allowlist is
    merged in — so auxiliary roles can be pinned to specific models without
    every call site knowing the configuration.
    """

    def __init__(
        self,
        registry: ModelSource,
        *,
        role_policies: Mapping[str, ModelPolicy] | None = None,
        usage_provider: Any = None,
    ) -> None:
        self._registry = registry
        self._role_policies: dict[str, ModelPolicy] = dict(role_policies or {})
        self._usage_provider = usage_provider
        self._admission = ModelAdmission()
        self._ranking = ModelRanking()
        self._stats_cache: (
            tuple[float, dict[tuple[str, str, str], tuple[int, int, float]]] | None
        ) = None

    def set_role_policy(self, role: str, policy: ModelPolicy) -> None:
        """Assign (or replace) the default policy for a role."""
        self._role_policies[role] = policy

    def provider_for(self, provider_name: str) -> object:
        """Expose the selected provider through the single routing authority.

        Callers that need to stream the selected model (for example the
        acceptance-judge verifier) must not reach around the router and keep
        a second provider registry.  Delegating here preserves the configured
        role-selection authority while retaining the registry's adapter lookup.
        """
        return self._registry.provider_for(provider_name)

    def effective_policy(self, policy: ModelPolicy | None = None) -> ModelPolicy:
        """Return task policy narrowed by the configured role policy."""
        return self._resolve_policy(policy or ModelPolicy())

    def _resolve_policy(self, policy: ModelPolicy) -> ModelPolicy:
        """Intersect role restrictions with the task policy.

        Role configuration is a candidate restriction, never an authority
        expansion.  In particular, a role cannot make an offline task remote,
        raise its cost ceiling, or replace a task allowlist with a broader one.
        Unknown roles fall back to the configured primary policy.
        """
        role_policy = self._role_policies.get(policy.role)
        if role_policy is None and policy.role != "primary":
            role_policy = self._role_policies.get("primary")
        if role_policy is None:
            return policy
        task_allowed = tuple(policy.allowed or ())
        role_allowed = tuple(role_policy.allowed or ())
        if task_allowed and role_allowed:
            allowed = tuple(item for item in task_allowed if item in role_allowed)
            if not allowed:
                allowed = _NO_MODELS
        else:
            allowed = task_allowed or role_allowed
        return replace(
            policy,
            allowed=allowed,
            require_tools=bool(policy.require_tools or role_policy.require_tools),
            privacy=_stricter_policy_privacy(policy.privacy, role_policy.privacy),
            max_cost_usd=_min_cost(policy.max_cost_usd, role_policy.max_cost_usd),
            min_quality_tier=_stricter_quality_tier(
                policy.min_quality_tier,
                role_policy.min_quality_tier,
            ),
            require_declared_quality=bool(
                policy.require_declared_quality or role_policy.require_declared_quality
            ),
            max_model_attempts=min(policy.max_model_attempts, role_policy.max_model_attempts),
            routing_preference=(
                policy.routing_preference
                if policy.routing_preference != "balanced"
                else role_policy.routing_preference
            ),
        )

    async def select(
        self,
        *,
        policy: ModelPolicy | None = None,
        requirements: ModelRequirements | None = None,
        exclude: frozenset[str | tuple[str, str]] = frozenset(),
    ) -> ModelSelection:
        policy = self._resolve_policy(policy or ModelPolicy())
        requirements = requirements or ModelRequirements()
        models = list(await self._effective_models())

        if not models:
            raise ProviderUnavailable("no model providers registered")
        ready_providers = self._ready_provider_names()
        admission = self._admission.admit(
            models,
            policy=policy,
            requirements=requirements,
            ready_providers=ready_providers,
            exclude=exclude,
        )
        if not admission.eligible:
            grouped = admission.by_reason()
            details = "; ".join(
                f"{reason} ({len(items)} candidate{'s' if len(items) != 1 else ''})"
                for reason, items in grouped.items()
            )
            capacity = next(iter(grouped.get("capacity", ())), None)
            capacity_detail = (
                f" required_context={requirements.minimum_context_window_tokens} "
                f"provider={capacity.provider} model={capacity.model} ({capacity.detail})"
                if capacity
                else ""
            )
            allowed = tuple(policy.allowed or ())
            offline = str(policy.privacy).lower() in {"local", "offline"}
            raise ModelUnavailable(
                f"no eligible model: {len(models)} candidates rejected — {details}. "
                f"(allowed={list(allowed)}, privacy={policy.privacy}, "
                f"offline={offline},{capacity_detail})",
                rejections=tuple(item.to_dict() for item in admission.rejected),
            )

        stats = await self._historical_stats(policy.role)
        candidates = admission.eligible
        history_used = any(
            (descriptor.provider, descriptor.model, policy.role) in stats
            for descriptor in candidates
        )
        best = self._ranking.choose(
            candidates,
            stats=stats,
            role=policy.role,
            routing_preference=policy.routing_preference,
        )
        return ModelSelection(
            provider=best.provider,
            model=best.model,
            info=best.info,
            rationale=self._ranking.rationale(
                best.info,
                policy=policy,
                requirements=requirements,
                history_used=history_used,
            ),
            effective=best,
        )

    async def _effective_models(self) -> Sequence[EffectiveModelDescriptor]:
        """Read the normalized inventory without creating a second authority."""
        return tuple(await self._registry.list_effective_models())

    def _ready_provider_names(self) -> set[str] | None:
        """Return provider names currently admitted for model selection.

        The readiness surface is optional for small compatibility registries;
        the production ``ProviderRegistry`` always supplies it. When present,
        only providers explicitly in ``ready`` state may contribute models.
        """
        probe = getattr(self._registry, "readiness", None)
        if not callable(probe):
            return None
        try:
            report = probe()
        except Exception:
            return set()
        providers = report.get("providers") if isinstance(report, Mapping) else None
        if not isinstance(providers, Mapping):
            return None
        return {
            str(name)
            for name, value in providers.items()
            if isinstance(value, Mapping) and str(value.get("state")) == "ready"
        }

    async def _historical_stats(
        self,
        role: str,
    ) -> dict[tuple[str, str, str], tuple[int, int, float]]:
        """Read a short rolling window of canonical provider attempt telemetry."""
        usage_source = self._usage_provider
        if usage_source is None or not hasattr(usage_source, "list_recent"):
            return {}
        now = time.monotonic()
        if self._stats_cache is not None and now - self._stats_cache[0] < 5.0:
            return {key: value for key, value in self._stats_cache[1].items() if key[2] == role}
        try:
            rows = await usage_source.list_recent(limit=500)
        except (OSError, RuntimeError, TypeError, ValueError):
            return {}
        stats: dict[tuple[str, str, str], tuple[int, int, float]] = {}
        for row in rows or ():
            if not isinstance(row, Mapping):
                continue
            metadata = row.get("metadata")
            if not isinstance(metadata, Mapping):
                continue
            row_role = str(metadata.get("role") or "primary")
            state = str(metadata.get("state") or "").lower()
            if state not in {"success", "failed"}:
                continue
            key = (
                str(row.get("provider") or ""),
                str(row.get("model") or ""),
                row_role,
            )
            attempts, successes, latency = stats.get(key, (0, 0, 0.0))
            duration = metadata.get("duration_ms")
            try:
                measured = max(
                    0.0,
                    float(duration) if isinstance(duration, (int, float, str)) else 0.0,
                )
            except (TypeError, ValueError):
                measured = 0.0
            stats[key] = (
                attempts + 1,
                successes + int(state == "success"),
                latency + measured,
            )
        self._stats_cache = (now, stats)
        return {key: value for key, value in stats.items() if key[2] == role}


__all__ = [
    "CAP_AUDIO_INPUT",
    "CAP_AUDIO_OUTPUT",
    "CAP_REASONING",
    "CAP_STREAMING",
    "CAP_STRUCTURED",
    "CAP_TOOLS",
    "CAP_VISION",
    "ModelRequirements",
    "ModelRouter",
    "ModelSelection",
    "ModelSource",
]


_POLICY_PRIVACY_RANK = {
    "offline": 0,
    "local": 0,
    "local-preferred": 1,
    "local-pref": 1,
    "remote": 2,
}


def _stricter_policy_privacy(left: str, right: str) -> str:
    left_value = str(left or "local-preferred").lower()
    right_value = str(right or "local-preferred").lower()
    left_rank = _POLICY_PRIVACY_RANK.get(left_value, 0)
    right_rank = _POLICY_PRIVACY_RANK.get(right_value, 0)
    return left if left_rank <= right_rank else right


def _min_cost(left, right):
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


def _stricter_quality_tier(left: str | None, right: str | None) -> str | None:
    """Intersect quality floors without allowing a role to weaken a task."""
    values = [value for value in (left, right) if value]
    valid: list[tuple[int, str]] = []
    for value in values:
        try:
            tier = ModelQualityTier(str(value))
        except ValueError:
            continue
        valid.append((tier.rank, tier.value))
    if valid:
        return max(valid)[1]
    return left or right
