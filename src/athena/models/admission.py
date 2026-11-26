"""Hard model admission and structured rejection diagnostics."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from athena.models.inventory import EffectiveModelDescriptor
from athena.protocol.models import ModelInfo, ModelQualityTier, ModelRequirements, PrivacyClass
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
_UNKNOWN_CONTEXT_FLOOR = 16_384
_UNKNOWN_OUTPUT_FLOOR = 4_096

__all__ = [
    "CAP_AUDIO_INPUT",
    "CAP_AUDIO_OUTPUT",
    "CAP_REASONING",
    "CAP_STREAMING",
    "CAP_STRUCTURED",
    "CAP_TOOLS",
    "CAP_VISION",
    "AdmissionResult",
    "ModelAdmission",
    "ModelRejection",
    "ModelRequirements",
]


@dataclass(frozen=True)
class ModelRejection:
    """Why one candidate failed hard admission."""

    reason: str
    provider: str
    model: str
    detail: str = ""

    @property
    def candidate(self) -> tuple[str, str, str]:
        return (self.reason, self.provider, self.model)

    def to_dict(self) -> dict[str, str]:
        result = {
            "reason": self.reason,
            "provider": self.provider,
            "model": self.model,
        }
        if self.detail:
            result["detail"] = self.detail
        return result


@dataclass(frozen=True)
class AdmissionResult:
    eligible: tuple[EffectiveModelDescriptor, ...]
    rejected: tuple[ModelRejection, ...]

    def by_reason(self) -> dict[str, tuple[ModelRejection, ...]]:
        grouped: dict[str, list[ModelRejection]] = {}
        for item in self.rejected:
            grouped.setdefault(item.reason, []).append(item)
        return {reason: tuple(items) for reason, items in grouped.items()}


class ModelAdmission:
    """Apply policy, capability, privacy, capacity, cost, and quality gates."""

    def admit(
        self,
        models: Sequence[EffectiveModelDescriptor],
        *,
        policy: ModelPolicy,
        requirements: ModelRequirements,
        ready_providers: set[str] | None = None,
        exclude: Iterable[str | tuple[str, str]] = (),
    ) -> AdmissionResult:
        allowed = tuple(policy.allowed or ())
        privacy_gate = self._privacy_gate(policy)
        excluded = set(exclude)
        eligible: list[EffectiveModelDescriptor] = []
        rejected: list[ModelRejection] = []
        for descriptor in models:
            info = descriptor.info
            if info.provider in excluded or (info.provider, info.id) in excluded:
                rejected.append(ModelRejection("excluded", info.provider, info.id))
                continue
            if ready_providers is not None and info.provider not in ready_providers:
                rejected.append(ModelRejection("provider_not_ready", info.provider, info.id))
                continue
            if allowed and not self._is_allowed(info, allowed):
                rejected.append(ModelRejection("not_allowed", info.provider, info.id))
                continue
            if not self._meets_cap(info, policy, requirements):
                rejected.append(ModelRejection("capability", info.provider, info.id))
                continue
            if not self._meets_capacity(info, requirements):
                rejected.append(
                    ModelRejection(
                        "capacity",
                        info.provider,
                        info.id,
                        self.capacity_reason(info, requirements),
                    )
                )
                continue
            if not self._meets_cost(info, policy, requirements):
                rejected.append(ModelRejection("cost", info.provider, info.id))
                continue
            if not privacy_gate(info):
                rejected.append(ModelRejection("privacy", info.provider, info.id))
                continue
            if not self._meets_quality_floor(info, policy):
                rejected.append(ModelRejection("quality", info.provider, info.id))
                continue
            eligible.append(descriptor)
        return AdmissionResult(tuple(eligible), tuple(rejected))

    @staticmethod
    def capacity_reason(info: ModelInfo, requirements: ModelRequirements) -> str:
        context = info.context_limit
        context_required = requirements.minimum_context_window_tokens
        if context_required is not None and (
            (context is None and context_required > _UNKNOWN_CONTEXT_FLOOR)
            or (context is not None and context < context_required)
        ):
            return "context capacity unknown" if context is None else "context capacity too small"
        output = info.max_output_tokens
        output_required = requirements.requested_output_tokens
        if output_required is not None and (
            (output is None and output_required > _UNKNOWN_OUTPUT_FLOOR)
            or (output is not None and output < output_required)
        ):
            return "output capacity unknown" if output is None else "output capacity too small"
        return "capacity policy rejected"

    @staticmethod
    def _is_allowed(info: ModelInfo, allowed: tuple[str, ...]) -> bool:
        return info.id in allowed or f"{info.provider}/{info.id}" in allowed

    @staticmethod
    def _meets_cap(info: ModelInfo, policy: ModelPolicy, requirements: ModelRequirements) -> bool:
        for cap in requirements.required_capabilities:
            attr = _CAPABILITY_FIELD.get(cap)
            if attr is not None and not getattr(info, attr, False):
                return False
        return not (policy.require_tools and not info.tool_calling)

    @staticmethod
    def _meets_capacity(info: ModelInfo, requirements: ModelRequirements) -> bool:
        if requirements.minimum_context_window_tokens is not None:
            limit = info.context_limit
            required = requirements.minimum_context_window_tokens
            if limit is None and required > _UNKNOWN_CONTEXT_FLOOR:
                return False
            if limit is not None and limit < required:
                return False
        if requirements.requested_output_tokens is not None:
            cap = info.max_output_tokens
            required = requirements.requested_output_tokens
            if cap is None and required > _UNKNOWN_OUTPUT_FLOOR:
                return False
            if cap is not None and cap < required:
                return False
        return True

    @staticmethod
    def _privacy_gate(policy: ModelPolicy):
        if str(policy.privacy).lower() in _OFFLINE_PRIVACY | {"local"}:
            return lambda info: info.privacy_class is PrivacyClass.LOCAL
        return lambda info: True

    @staticmethod
    def _meets_quality_floor(info: ModelInfo, policy: ModelPolicy) -> bool:
        raw = getattr(policy, "min_quality_tier", None)
        require_declared = bool(getattr(policy, "require_declared_quality", False))
        if not raw and not require_declared:
            return True
        tier = getattr(info, "quality_tier", ModelQualityTier.UNDECLARED)
        if isinstance(tier, str):
            try:
                tier = ModelQualityTier(tier)
            except ValueError:
                return not require_declared if not raw else True
        if not raw:
            return tier is not ModelQualityTier.UNDECLARED
        try:
            floor = ModelQualityTier(str(raw))
        except ValueError:
            return True
        if tier is ModelQualityTier.UNDECLARED:
            return not require_declared
        return tier.rank >= floor.rank

    @staticmethod
    def _meets_cost(info: ModelInfo, policy: ModelPolicy, requirements: ModelRequirements) -> bool:
        if policy.max_cost_usd is None:
            return True
        cost = info.cost
        if cost is None:
            return False
        if (
            cost.currency.upper() != "USD"
            or cost.per_1m_input is None
            or cost.per_1m_output is None
        ):
            return False
        input_tokens = max(int(requirements.estimated_input_tokens), 0)
        output_tokens = max(int(requirements.requested_output_tokens or 0), 0)
        if not input_tokens and not output_tokens:
            return True
        estimate = (
            cost.per_1m_input * input_tokens + cost.per_1m_output * output_tokens
        ) / 1_000_000
        return estimate <= float(policy.max_cost_usd)
