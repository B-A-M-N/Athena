"""ModelRouter — policy-driven provider/model selection (BUILDSPEC 26-27, BHV-034..038).

Selection is a pure function over declared ``ModelInfo``; the router holds NO
provider knowledge (INV-006). Capability filtering (BHV-034), provider
neutrality (BHV-035), privacy/offline discipline (BHV-037/038) and cost are
evaluated here. Fallback is deterministic and inspectable — it never silently
crosses a privacy, locality, or cost boundary that policy has not authorized.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
import time
from typing import Any, Protocol

from athena.protocol.errors import ModelUnavailable, ProviderUnavailable
from athena.protocol.models import ModelInfo, PrivacyClass
from athena.protocol.tasks import ModelPolicy

CAP_TOOLS = "tools"
CAP_VISION = "vision"
CAP_AUDIO_INPUT = "audio_input"
CAP_AUDIO_OUTPUT = "audio_output"
CAP_REASONING = "reasoning"
CAP_STRUCTURED = "structured"
CAP_STREAMING = "streaming"

_CAPABILITY_FIELD: dict[str, str] = {
    CAP_TOOLS: "tool_calling",
    CAP_VISION: "vision",
    CAP_AUDIO_INPUT: "audio_input",
    CAP_AUDIO_OUTPUT: "audio_output",
    CAP_REASONING: "reasoning",
    CAP_STRUCTURED: "structured_output",
    CAP_STREAMING: "streaming",
}

_OFFLINE_PRIVACY = frozenset({"local", "offline"})
_NO_MODELS = ("__athena_no_model_intersection__",)

_PRIVACY_RANK: dict[PrivacyClass, int] = {
    PrivacyClass.LOCAL: 0,
    PrivacyClass.UNKNOWN: 1,
    PrivacyClass.HYBRID: 2,
    PrivacyClass.REMOTE: 3,
}

_LATENCY_CLASS_RANK = {"fast": 0, "medium": 1, "slow": 2}
_ROUTING_PREFERENCES = frozenset({"balanced", "latency", "cost"})


@dataclass(frozen=True)
class ModelRequirements:
    """Declarative selection requirements (BUILDSPEC 27). All optional."""

    required_capabilities: frozenset[str] = frozenset()
    minimum_context_tokens: int | None = None
    max_output_tokens: int | None = None
    needs_tools: bool = False
    vision: bool = False
    audio: bool = False
    reasoning: bool = False
    reserved_output: int = 0


@dataclass(frozen=True)
class ModelSelection:
    provider: str
    model: str
    info: ModelInfo
    rationale: tuple[str, ...] = ()

    def __str__(self) -> str:
        return f"{self.provider}/{self.model}"


class ModelSource(Protocol):
    """Registry surface the router depends on."""

    async def list_models(self) -> Sequence[ModelInfo]: ...

    def provider_for(self, provider_name: str) -> object: ...


def _privacy_rank(cls: PrivacyClass) -> int:
    return _PRIVACY_RANK.get(cls, _PRIVACY_RANK[PrivacyClass.UNKNOWN])


def _cost_per_1m(info: ModelInfo) -> float:
    cost = info.cost
    if cost is None:
        return 0.0
    return float(cost.per_1m_input or 0) + float(cost.per_1m_output or 0)


def _candidate_key(info: ModelInfo) -> tuple:
    return (_privacy_rank(info.privacy_class), _cost_per_1m(info), f"{info.provider}/{info.id}")


def _declared_latency_rank(info: ModelInfo) -> int:
    """Return a conservative cold-start latency prior for a model."""
    return _LATENCY_CLASS_RANK.get(str(info.latency_class or "medium").casefold(), 1)


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
        exclude: frozenset[str] = frozenset(),
    ) -> ModelSelection:
        policy = self._resolve_policy(policy or ModelPolicy())
        requirements = requirements or ModelRequirements()
        models = list(await self._registry.list_models())

        if not models:
            raise ProviderUnavailable("no model providers registered")

        offline = policy.privacy in _OFFLINE_PRIVACY
        allowed = tuple(policy.allowed or ())
        privacy_gate = self._privacy_gate(policy)

        candidates: list[ModelInfo] = []
        for info in models:
            if info.provider in exclude:
                continue
            if allowed and not self._is_allowed(info, allowed):
                continue
            if not self._meets_cap(info, policy, requirements):
                continue
            if not self._meets_capacity(info, requirements):
                continue
            if not self._meets_cost(info, policy):
                continue
            if not privacy_gate(info):
                continue
            candidates.append(info)

        if not candidates:
            raise ModelUnavailable(
                "no model satisfies policy "
                f"(allowed={list(allowed)}, privacy={policy.privacy}, "
                f"offline={offline})"
            )

        stats = await self._historical_stats(policy.role)
        history_used = any((info.provider, info.id, policy.role) in stats for info in candidates)
        best = min(
            candidates,
            key=lambda info: self._selection_key(
                info, stats, policy.role, policy.routing_preference
            ),
        )
        return ModelSelection(
            provider=best.provider,
            model=best.id,
            info=best,
            rationale=self._rationale(
                best,
                policy,
                requirements,
                history_used=history_used,
            ),
        )

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

    @staticmethod
    def _selection_key(
        info: ModelInfo,
        stats: Mapping[tuple[str, str, str], tuple[int, int, float]],
        role: str,
        routing_preference: str = "balanced",
    ) -> tuple:
        attempts, successes, total_latency = stats.get((info.provider, info.id, role), (0, 0, 0.0))
        # Use a small conservative prior (three successes, one failure).
        # One transient failure therefore influences routing without making a
        # previously viable provider permanently lose the role.
        reliability_penalty = (attempts - successes + 1) / (attempts + 4) if attempts else 0.25
        # Measured latency wins once a role has telemetry.  Before that, use
        # the provider's declared class so a cold router does not select a
        # known slow model merely because it has no history yet.
        latency_observed = 0 if attempts else 1
        latency_value = total_latency / attempts if attempts else _declared_latency_rank(info)
        preference = str(routing_preference or "balanced").casefold()
        if preference not in _ROUTING_PREFERENCES:
            preference = "balanced"
        operational: tuple[Any, ...]
        if preference == "cost":
            operational = (_cost_per_1m(info), latency_observed, latency_value)
        elif preference == "latency":
            operational = (latency_observed, latency_value, _cost_per_1m(info))
        else:
            # Balanced keeps observed reliability first, then avoids a cold
            # route to a declared slow model before using cost as a tie-break.
            operational = (latency_observed, latency_value, _cost_per_1m(info))
        return (
            _privacy_rank(info.privacy_class),
            reliability_penalty,
            *operational,
            f"{info.provider}/{info.id}",
        )

    def _privacy_gate(self, policy: ModelPolicy) -> Callable[[ModelInfo], bool]:
        if str(policy.privacy).lower() in _OFFLINE_PRIVACY | {"local"}:
            return lambda info: info.privacy_class is PrivacyClass.LOCAL
        return lambda info: True

    def _is_allowed(self, info: ModelInfo, allowed: tuple[str, ...]) -> bool:
        return info.id in allowed or f"{info.provider}/{info.id}" in allowed

    def _meets_cap(
        self, info: ModelInfo, policy: ModelPolicy, requirements: ModelRequirements
    ) -> bool:
        for cap in requirements.required_capabilities:
            attr = _CAPABILITY_FIELD.get(cap)
            if attr is not None and not getattr(info, attr, False):
                return False
        return not (policy.require_tools and not info.tool_calling)

    def _meets_capacity(self, info: ModelInfo, requirements: ModelRequirements) -> bool:
        if requirements.minimum_context_tokens is not None:
            limit = info.context_limit
            if limit is not None and limit < requirements.minimum_context_tokens:
                return False
        if requirements.max_output_tokens is not None:
            cap = info.max_output_tokens
            if cap is not None and cap < requirements.max_output_tokens:
                return False
        return True

    def _meets_cost(self, info: ModelInfo, policy: ModelPolicy) -> bool:
        if policy.max_cost_usd is None:
            return True
        cost = info.cost
        if cost is None:
            # Under a strict cost ceiling, unknown pricing is NOT equivalent to free.
            # Treat as unavailable if the policy has a max_cost_usd constraint.
            return False
        if (
            cost.currency.upper() != "USD"
            or cost.per_1m_input is None
            or cost.per_1m_output is None
        ):
            # A partial rate card, or a currency we cannot compare to the USD
            # policy ceiling, is unknown rather than free.
            return False
        # Estimate based on typical request sizes (conservative: assume 10k input, 4k output)
        estimate = cost.per_1m_input * 0.01 + cost.per_1m_output * 0.004
        return estimate <= float(policy.max_cost_usd)

    def _rationale(
        self,
        best: ModelInfo,
        policy: ModelPolicy,
        requirements: ModelRequirements,
        *,
        history_used: bool = False,
    ) -> tuple[str, ...]:
        parts = [f"model={best.id}", f"provider={best.provider}"]
        if policy.privacy:
            parts.append(f"privacy={best.privacy_class.value}")
        if requirements.required_capabilities:
            parts.append("caps=" + ",".join(sorted(requirements.required_capabilities)))
        if history_used:
            parts.append("history=rolling_attempts")
        parts.append(f"preference={policy.routing_preference}")
        return tuple(parts)


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
