"""ProviderRegistry — registration of ModelProvider adapters and model lookups.

The router consumes this registry purely through ``list_models`` and
``provider_for`` (see router.ModelSource). All provider instantiation lives
with the caller; the registry holds adapter instances and their declared models.
"""

from __future__ import annotations

from typing import AsyncIterator, Sequence

from athena.protocol.errors import ModelUnavailable, ProviderUnavailable
from athena.protocol.models import (
    ModelEvent,
    ModelInfo,
    ModelProvider,
    ModelRequest,
    ModelResponse,
)


class ProviderRegistry:
    """Maps provider names to ModelProvider adapter instances."""

    def __init__(self) -> None:
        self._providers: dict[str, ModelProvider] = {}

    def register(self, provider_name: str, provider: ModelProvider) -> None:
        if not isinstance(provider, object) or not hasattr(provider, "complete"):
            raise ProviderUnavailable(f"provider {provider_name!r} is not a ModelProvider")
        self._providers[provider_name] = provider

    def unregister(self, provider_name: str) -> None:
        self._providers.pop(provider_name, None)

    def names(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def __contains__(self, provider_name: str) -> bool:
        return provider_name in self._providers

    def provider_for(self, provider_name: str) -> ModelProvider:
        try:
            return self._providers[provider_name]
        except KeyError:
            raise ProviderUnavailable(f"provider {provider_name!r} is not registered")

    async def list_models(self) -> Sequence[ModelInfo]:
        out: list[ModelInfo] = []
        for name, provider in self._providers.items():
            for info in await provider.list_models():
                if info.provider == "":
                    info = _with_provider(info, name)
                out.append(info)
        return out

    async def resolve(self, provider_name: str, model_name: str) -> ModelInfo:
        provider = self.provider_for(provider_name)
        for info in await provider.list_models():
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
        return await _collect_response(provider, request)


def _with_provider(info: ModelInfo, provider_name: str) -> ModelInfo:
    fields = {
        k: getattr(info, k)
        for k in {
            "id", "context_limit", "max_output_tokens", "tool_calling", "vision",
            "audio_input", "audio_output", "reasoning", "structured_output",
            "streaming", "cost", "latency_class", "privacy_class",
        }
    }
    return ModelInfo(provider=provider_name, **fields)


async def _collect_response(provider: ModelProvider, request: ModelRequest) -> ModelResponse:
    response: ModelResponse | None = None
    async for event in provider.complete(request):
        if event.response is not None:
            response = event.response
    if response is None:
        raise ModelUnavailable(f"provider produced no response for {request.request_id}")
    return response


__all__ = ["ProviderRegistry"]