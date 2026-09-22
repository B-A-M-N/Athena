"""ProviderRegistry — registration of ModelProvider adapters and model lookups.

The router consumes this registry through the normalized
``list_effective_models`` inventory and ``provider_for`` (see
router.ModelSource). All provider instantiation lives with the caller; the
registry holds adapter instances and their declared models.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
import time

from athena.protocol.errors import ModelUnavailable, ProviderUnavailable
from athena.protocol.models import (
    ModelEvent,
    ModelInfo,
    ModelProvider,
    ModelRequest,
    ModelResponse,
)
from athena.models.inventory import EffectiveModelDescriptor, normalize_model
from athena.models.response_collection import collect_response


class ProviderRegistry:
    """Maps provider names to ModelProvider adapter instances."""

    def __init__(self) -> None:
        self._providers: dict[str, ModelProvider] = {}
        self._profiles: dict[str, object] = {}
        # Route profiles describe wire/auth/cache semantics. Model profiles
        # describe selected-model quirks. Both belong to this one registry so
        # the kernel, adapters, and repair boundary share an authority.
        self._model_profiles: dict[tuple[str, str], object] = {}
        self._models_cache: tuple[float, tuple[EffectiveModelDescriptor, ...]] | None = None
        self._models_cache_ttl = 2.0
        self._generation = 0

    @property
    def generation(self) -> int:
        """Monotonic inventory revision for consumers caching route data."""
        return self._generation

    def _invalidate_models(self) -> None:
        self._models_cache = None
        self._generation += 1

    def register(self, provider_name: str, provider: ModelProvider) -> None:
        if not isinstance(provider, object) or not hasattr(provider, "complete"):
            raise ProviderUnavailable(f"provider {provider_name!r} is not a ModelProvider")
        self._providers[provider_name] = provider
        self._invalidate_models()

    def unregister(self, provider_name: str) -> None:
        self._providers.pop(provider_name, None)
        self._profiles.pop(provider_name, None)
        self._model_profiles = {
            key: value for key, value in self._model_profiles.items() if key[0] != provider_name
        }
        self._invalidate_models()

    def set_profile(self, provider_name: str, profile: object) -> None:
        if provider_name not in self._providers:
            raise ProviderUnavailable(f"provider {provider_name!r} is not registered")
        self._profiles[provider_name] = profile
        self._invalidate_models()

    def profile_for(self, provider_name: str) -> object | None:
        return self._profiles.get(provider_name)

    def set_model_profile(
        self,
        provider_name: str,
        model_name: str,
        profile: object,
    ) -> None:
        if provider_name not in self._providers:
            raise ProviderUnavailable(f"provider {provider_name!r} is not registered")
        if not model_name:
            raise ValueError("model profile requires a model name")
        self._model_profiles[(provider_name, model_name)] = profile
        self._invalidate_models()

    def model_profile_for(self, provider_name: str, model_name: str) -> object | None:
        return self._model_profiles.get((provider_name, model_name))

    def names(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def readiness(self) -> dict[str, object]:
        """Return usable provider states, not merely registered names."""
        providers: dict[str, dict[str, object]] = {}
        for name, provider in self._providers.items():
            probe = getattr(provider, "readiness", None)
            try:
                value = probe() if callable(probe) else {"state": "unverified"}
            except Exception as exc:  # readiness must remain diagnostic
                value = {"state": "degraded", "error": str(exc)}
            state = (
                str(value.get("state", "unverified")) if isinstance(value, dict) else "unverified"
            )
            providers[name] = {
                **(dict(value) if isinstance(value, dict) else {}),
                "state": state,
            }
        states = {str(item.get("state")) for item in providers.values()}
        if "ready" in states:
            overall = "ready"
        elif not providers:
            overall = "unconfigured"
        else:
            overall = "degraded"
        return {"state": overall, "providers": providers}

    def __contains__(self, provider_name: str) -> bool:
        return provider_name in self._providers

    def provider_for(self, provider_name: str) -> ModelProvider:
        try:
            return self._providers[provider_name]
        except KeyError:
            raise ProviderUnavailable(f"provider {provider_name!r} is not registered")

    async def list_models(self) -> Sequence[ModelInfo]:
        """Return the normalized inventory in its legacy model-only shape."""
        return tuple(item.info for item in await self.list_effective_models())

    async def list_effective_models(self) -> Sequence[EffectiveModelDescriptor]:
        """Return one canonical descriptor per discovered provider model."""
        now = time.monotonic()
        if self._models_cache is not None and now - self._models_cache[0] < self._models_cache_ttl:
            return self._models_cache[1]
        out: list[EffectiveModelDescriptor] = []
        for name, provider in self._providers.items():
            for info in await provider.list_models():
                # The registration key is the canonical identity: an adapter
                # must not shadow routing with a different nonempty name.
                info = _with_provider(info, name)
                out.append(
                    normalize_model(
                        info,
                        provider_profile=self.profile_for(info.provider),
                        model_profile=self.model_profile_for(info.provider, info.id),
                    )
                )
        result = tuple(out)
        self._models_cache = (now, result)
        return result

    async def refresh_models(self) -> Sequence[ModelInfo]:
        """Refresh the provider inventory once, bypassing the TTL cache."""
        self._invalidate_models()
        return await self.list_models()

    async def resolve(self, provider_name: str, model_name: str) -> ModelInfo:
        self.provider_for(provider_name)
        for info in await self.list_models():
            if info.provider != provider_name:
                continue
            if info.id == model_name or f"{provider_name}/{info.id}" == model_name:
                return info
        raise ModelUnavailable(f"model {model_name!r} not offered by {provider_name!r}")

    async def complete(
        self, provider_name: str, request: ModelRequest
    ) -> AsyncIterator[ModelEvent]:
        provider = self.provider_for(provider_name)
        async for event in provider.complete(request):
            yield event

    async def invoke(self, provider_name: str, request: ModelRequest) -> ModelResponse:
        """Accumulate a provider stream into a single ModelResponse."""
        provider = self.provider_for(provider_name)
        return await collect_response(provider, request)


def _with_provider(info: ModelInfo, provider_name: str) -> ModelInfo:
    return replace(info, provider=provider_name)


_collect_response = collect_response


__all__ = ["ProviderRegistry"]
