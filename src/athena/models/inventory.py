"""Canonical model inventory records assembled before routing decisions."""

from __future__ import annotations

from dataclasses import dataclass, replace

from athena.protocol.models import ModelInfo

__all__ = ["EffectiveModelDescriptor", "normalize_model"]


@dataclass(frozen=True)
class EffectiveModelDescriptor:
    """One model's effective metadata at the provider/model boundary."""

    info: ModelInfo
    provider_profile: object | None = None
    model_profile: object | None = None

    @property
    def provider(self) -> str:
        return self.info.provider

    @property
    def model(self) -> str:
        return self.info.id


def normalize_model(
    info: ModelInfo,
    *,
    provider_profile: object | None = None,
    model_profile: object | None = None,
) -> EffectiveModelDescriptor:
    """Merge profile-derived capacity into provider-discovered metadata.

    Provider discovery remains the source for declared capabilities. Profiles
    may fill missing deployment capacity, but never overwrite discovered
    values. The resulting descriptor is the only record admission needs.
    """
    if model_profile is not None:
        context_window = getattr(model_profile, "context_window", None)
        output_limit = getattr(model_profile, "output_limit", None)
        info = replace(
            info,
            context_limit=(
                info.context_limit if info.context_limit is not None else context_window
            ),
            max_output_tokens=(
                info.max_output_tokens if info.max_output_tokens is not None else output_limit
            ),
        )
    return EffectiveModelDescriptor(
        info=info,
        provider_profile=provider_profile,
        model_profile=model_profile,
    )
