"""ModelRouter — policy-driven provider/model selection (BUILDSPEC 26-27, BHV-034..038).

Selection is a pure function over declared ``ModelInfo``; the router holds NO
provider knowledge (INV-006). Capability filtering (BHV-034), provider
neutrality (BHV-035), privacy/offline discipline (BHV-037/038) and cost are
evaluated here. Fallback is deterministic and inspectable — it never silently
crosses a privacy, locality, or cost boundary that policy has not authorized.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Mapping, Protocol, Sequence

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

_PRIVACY_RANK: dict[PrivacyClass, int] = {
    PrivacyClass.LOCAL: 0,
    PrivacyClass.UNKNOWN: 1,
    PrivacyClass.HYBRID: 2,
    PrivacyClass.REMOTE: 3,
}


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
    ) -> None:
        self._registry = registry
        self._role_policies: dict[str, ModelPolicy] = dict(role_policies or {})

    def set_role_policy(self, role: str, policy: ModelPolicy) -> None:
        """Assign (or replace) the default policy for a role."""
        self._role_policies[role] = policy

    def _resolve_policy(self, policy: ModelPolicy) -> ModelPolicy:
        """Merge role defaults into a caller-supplied policy.

        A caller's explicit allowlist always wins; role defaults only fill in
        when the caller did not constrain models itself. Unknown roles fall
        back to the "primary" role policy if one exists (user's global choice).
        """
        allowed = tuple(policy.allowed or ())
        if allowed:
            return policy
        role_policy = self._role_policies.get(policy.role)
        if role_policy is None and policy.role != "primary":
            role_policy = self._role_policies.get("primary")
        if role_policy is None:
            return policy
        return replace(
            policy,
            allowed=tuple(role_policy.allowed or ()),
            privacy=role_policy.privacy or policy.privacy,
            max_cost_usd=role_policy.max_cost_usd or policy.max_cost_usd,
        )

    async def select(
        self,
        *,
        policy: ModelPolicy | None = None,
        requirements: ModelRequirements | None = None,
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

        best = sorted(candidates, key=_candidate_key)[0]
        return ModelSelection(
            provider=best.provider,
            model=best.id,
            info=best,
            rationale=self._rationale(best, policy, requirements),
        )

    def _privacy_gate(self, policy: ModelPolicy) -> Callable[[ModelInfo], bool]:
        if policy.privacy in _OFFLINE_PRIVACY:
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
        if policy.require_tools and not info.tool_calling:
            return False
        return True

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
        # Estimate based on typical request sizes (conservative: assume 10k input, 4k output)
        estimate = (cost.per_1m_input or 0.0) * 0.01 + (cost.per_1m_output or 0.0) * 0.004
        return estimate <= float(policy.max_cost_usd)

    def _rationale(
        self,
        best: ModelInfo,
        policy: ModelPolicy,
        requirements: ModelRequirements,
    ) -> tuple[str, ...]:
        parts = [f"model={best.id}", f"provider={best.provider}"]
        if policy.privacy:
            parts.append(f"privacy={best.privacy_class.value}")
        if requirements.required_capabilities:
            parts.append("caps=" + ",".join(sorted(requirements.required_capabilities)))
        return tuple(parts)


__all__ = [
    "ModelRequirements",
    "ModelSelection",
    "ModelSource",
    "ModelRouter",
    "CAP_TOOLS",
    "CAP_VISION",
    "CAP_AUDIO_INPUT",
    "CAP_AUDIO_OUTPUT",
    "CAP_REASONING",
    "CAP_STRUCTURED",
    "CAP_STREAMING",
]