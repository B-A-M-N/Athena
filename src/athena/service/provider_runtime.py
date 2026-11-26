"""Provider construction mechanism owned by :class:`AthenaService`.

The service remains the composition root and public facade.  This module only
contains provider registration/construction mechanics; it does not select a
model, run inference, or create another decision loop.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from functools import partial
from typing import Any

from athena.models.compat.profiles import ModelProfile, resolve_profile
from athena.models.credentials import ProviderCredentialPool
from athena.models.providers.anthropic import AnthropicProvider
from athena.models.providers.fake import FakeModelProvider
from athena.models.providers.openai_compat import OpenAICompatProvider
from athena.models.registry import ProviderRegistry
from athena.policy.credentials import SecretError
from athena.service.config import ProviderConfig
from athena.service.provider_runtime_ports import ProviderRuntimePorts

_logger = logging.getLogger("athena.service.provider_runtime")


class ProviderRuntime:
    def __init__(self, service: Any, *, ports: ProviderRuntimePorts | None = None) -> None:
        self._ports = ports or ProviderRuntimePorts(service)

    def register(self, registry: ProviderRegistry) -> None:
        for config in tuple(self._ports.config.providers):
            provider: Any = None
            if config.kind == "fake":
                provider = FakeModelProvider(
                    tool_calling=True,
                    model=config.model,
                    provider=config.name,
                    scripts=list(config.extra.get("scripts") or []),
                    vision=bool(config.extra.get("vision", False)),
                    audio_input=bool(config.extra.get("audio_input", False)),
                    audio_output=bool(config.extra.get("audio_output", False)),
                    cost=config.extra.get("cost"),
                    latency_class=config.latency_class or config.extra.get("latency_class"),
                )
                registry.register(config.name, provider)
                registry.set_profile(config.name, resolve_profile("fake", model_id=config.model))
                registry.set_model_profile(
                    config.name,
                    config.model,
                    _model_profile_from_config(config.model, config.extra.get("model_profile")),
                )
                continue

            credential_ids = tuple(
                dict.fromkeys(
                    str(value)
                    for value in (
                        config.credential_ids
                        or ((config.credential_id,) if config.credential_id else ())
                    )
                    if value
                )
            )
            if len(credential_ids) > 1:

                def build_slot_provider(
                    credential_id: str,
                    provider_config: ProviderConfig = config,
                ) -> Any:
                    return self.build(provider_config, credential_id)

                provider = ProviderCredentialPool(
                    config.name,
                    [
                        (
                            credential_id,
                            partial(build_slot_provider, credential_id),
                        )
                        for credential_id in credential_ids
                    ],
                )
            else:
                provider = self.build(
                    config,
                    credential_ids[0] if credential_ids else None,
                )
            profile = resolve_profile(
                config.kind,
                base_url=config.base_url,
                model_id=config.model,
                cache_mode=config.cache_mode,
            )
            registry.register(config.name, provider)
            registry.set_profile(config.name, profile)
            registry.set_model_profile(
                config.name,
                profile.model_id or config.model,
                _model_profile_from_config(
                    profile.model_id or config.model,
                    config.extra.get("model_profile"),
                ),
            )

    def build(self, config: ProviderConfig, credential_id: str | None = None) -> Any:
        """Construct one provider adapter for one credential slot."""
        profile = resolve_profile(
            config.kind,
            base_url=config.base_url,
            model_id=config.model,
            cache_mode=config.cache_mode,
        )
        if profile.protocol in {"openai", "openai-compat"}:
            if not profile.base_url:
                raise ValueError(f"provider {config.name!r} needs an explicit base_url")
            return OpenAICompatProvider(
                base_url=profile.base_url,
                api_key=self.resolve_api_key(config, credential_id=credential_id),
                model=profile.model_id or config.model,
                provider=config.name,
                headers=config.extra.get("headers"),
                timeout=float(config.extra.get("timeout", 60.0)),
                http2=bool(config.extra.get("http2", False)),
                authentication=config.authentication,
                cost=config.extra.get("cost"),
                latency_class=config.latency_class or config.extra.get("latency_class"),
                vision=bool(config.extra.get("vision", False)),
                audio_input=bool(config.extra.get("audio_input", False)),
                audio_output=bool(config.extra.get("audio_output", False)),
                allow_insecure_remote=bool(config.extra.get("allow_insecure_remote", False)),
                trust_env=bool(config.extra.get("trust_env", False)),
            )
        if profile.protocol == "anthropic":
            return AnthropicProvider(
                api_key=self.resolve_api_key(config, credential_id=credential_id) or None,
                base_url=profile.base_url,
                model=profile.model_id or config.model,
                provider=config.name,
                headers=config.extra.get("headers"),
                timeout=float(config.extra.get("timeout", 60.0)),
                use_sdk=bool(config.extra.get("use_sdk", True)),
                cost=config.extra.get("cost"),
                latency_class=config.latency_class or config.extra.get("latency_class"),
                allow_insecure_remote=bool(config.extra.get("allow_insecure_remote", False)),
                trust_env=bool(config.extra.get("trust_env", False)),
            )
        raise ValueError(f"unsupported provider protocol: {profile.protocol!r}")

    def resolve_api_key(
        self,
        config: ProviderConfig,
        *,
        credential_id: str | None = None,
    ) -> str:
        """Resolve a leased credential at the provider boundary."""
        selected = credential_id or config.credential_id
        secrets = self._ports.secrets
        if selected and secrets is not None:
            try:
                return secrets.resolve(selected)
            except SecretError as exc:
                _logger.warning("provider credential %s unavailable: %s", selected, exc)
                return ""
        return config.api_key or ""


def _model_profile_from_config(model_id: str, raw: Any) -> ModelProfile:
    """Build a strict behavioral model profile from provider configuration."""
    if raw is None:
        return ModelProfile(model_pattern=model_id)
    if not isinstance(raw, Mapping):
        raise ValueError("provider model_profile must be a mapping")
    allowed = {
        "tools_structured",
        "tools_parallel",
        "tools_textual_fallback",
        "reasoning_native",
        "empty_content_with_tools",
        "requires_tool_result_name",
        "requires_assistant_replay_fields",
        "malformed_json_tendency",
        "context_window",
        "output_limit",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError("unknown model_profile fields: " + ", ".join(sorted(unknown)))
    return ModelProfile(model_pattern=model_id, **dict(raw))


__all__ = ["ProviderRuntime", "ProviderRuntimePorts"]
