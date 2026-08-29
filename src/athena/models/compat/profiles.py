"""Inference Compatibility Kernel — profiles and presets.

The model boundary is a kernel, not a pile of endpoint conditionals:
Athena understands MODEL BEHAVIOR via declared/discovered capability
profiles rather than merely knowing an API URL.

ProviderProfile  — protocol, endpoint, auth, roles, capabilities,
                   cache behavior, timeout classes, compatibility profile.
ModelProfile     — model-specific quirks (empty content with tools,
                   malformed JSON tendency, textual tool-call formats...).
CompatibilityProfile — named repair/replay/streaming behavior set.

Presets cover Ollama / LM Studio / vLLM / llama.cpp-compatible servers
plus hosted providers; base_url and model_id stay independent so custom
endpoints never require a code fork.

Profiles are immutable for the lifetime of an in-flight call and referenced
by stable id in the session record (replay fidelity).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

__all__ = [
    "COMPATIBILITY_PRESETS",
    "PRESETS",
    "AuthMode",
    "CacheMode",
    "CompatibilityProfile",
    "DiscoveryMode",
    "ModelProfile",
    "Protocol",
    "ProviderProfile",
    "profile_fingerprint",
    "resolve_compatibility_profile",
]


class Protocol:
    OPENAI = "openai"
    OPENAI_COMPAT = "openai-compat"
    ANTHROPIC = "anthropic"


class AuthMode:
    REQUIRED_KEY = "required-key"
    OPTIONAL_KEY = "optional-key"
    KEYLESS = "keyless"
    BEARER = "bearer"


class CacheMode:
    AUTOMATIC_PREFIX = "automatic-prefix"
    EXPLICIT_CACHE_API = "explicit-cache-api"
    SESSION_KEY = "session-key"
    NONE = "none"


class DiscoveryMode:
    OFF = "off"
    MANUAL = "manual"
    AUTOMATIC = "automatic"


@dataclass(frozen=True)
class ProviderProfile:
    """Immutable description of one provider/local-server route."""

    id: str
    protocol: str = Protocol.OPENAI_COMPAT
    base_url: str = ""
    model_id: str = ""
    auth_mode: str = AuthMode.OPTIONAL_KEY
    api_key_ref: str | None = None  # env var NAME, never the value
    roles: frozenset[str] = frozenset({"system"})
    capabilities: frozenset[str] = frozenset({"streaming", "tools"})
    cache_mode: str = CacheMode.NONE
    cache_session_key: bool = True
    timeouts: Mapping[str, float] = field(
        default_factory=lambda: {
            "connect": 10.0,
            "first_event": 120.0,
            "idle": 300.0,
            "total": 1800.0,
        }
    )
    compatibility_profile: str = "auto"
    discovery_mode: str = DiscoveryMode.MANUAL

    def fingerprint(self) -> str:
        return profile_fingerprint(self)


@dataclass(frozen=True)
class ModelProfile:
    """Declared/observed behavioral quirks for one model family."""

    model_pattern: str  # prefix match on model id
    tools_structured: bool = True
    tools_parallel: bool = True
    tools_textual_fallback: bool = False  # emits pseudo-XML tool calls
    reasoning_native: bool = False
    empty_content_with_tools: bool = False  # sends "" content alongside calls
    requires_tool_result_name: bool = False
    requires_assistant_replay_fields: bool = False
    malformed_json_tendency: bool = False  # benefit from repair pass
    context_window: int | None = None
    output_limit: int | None = None
    # Conservative hard-admission bound. ``None`` means this model profile
    # cannot safely bound tokens and finite hard input budgets must refuse it.
    token_upper_bound_per_byte: int | None = 1


@dataclass(frozen=True)
class CompatibilityProfile:
    """Named repair/replay behavior applied at the model boundary."""

    id: str
    tool_repair: str = "safe"  # safe | strict | off
    max_tool_correction_cycles: int = 2
    textual_tool_calls: bool = True  # extract textual candidates
    preserve_reasoning_replay: bool = True
    strict_mcp_aliases: bool = True  # MCP gets no fuzzy aliases


COMPATIBILITY_PRESETS: dict[str, CompatibilityProfile] = {
    "auto": CompatibilityProfile("auto"),
    "test": CompatibilityProfile("test"),
    "local": CompatibilityProfile("local"),
    "hosted": CompatibilityProfile("hosted"),
}


def resolve_compatibility_profile(profile_id: str) -> CompatibilityProfile:
    """Resolve a named correction/replay policy without a silent fallback."""
    try:
        return COMPATIBILITY_PRESETS[profile_id]
    except KeyError:
        known = ", ".join(sorted(COMPATIBILITY_PRESETS))
        raise ValueError(f"unknown compatibility profile {profile_id!r}; known: {known}") from None


# ---------------------------------------------------------------------------
# Fingerprints (stable serialization -> cache identity + schema hashes)
# ---------------------------------------------------------------------------


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def profile_fingerprint(profile: ProviderProfile) -> str:
    data = {
        # The profile id is part of the replay/cache boundary even when two
        # routes currently happen to share identical wire settings. A later
        # profile revision must not silently reuse the old boundary.
        "id": profile.id,
        "protocol": profile.protocol,
        "base_url": profile.base_url,
        "model_id": profile.model_id,
        "auth_mode": profile.auth_mode,
        "roles": sorted(profile.roles),
        "capabilities": sorted(profile.capabilities),
        "cache_mode": profile.cache_mode,
        "cache_session_key": profile.cache_session_key,
        "timeouts": dict(sorted(profile.timeouts.items())),
        "compatibility_profile": profile.compatibility_profile,
        "discovery_mode": profile.discovery_mode,
        "api_key_ref": profile.api_key_ref,
    }
    return hashlib.sha256(_canonical_json(data).encode()).hexdigest()[:16]


def schema_fingerprint(schema: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(dict(schema)).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Local/OpenAI-compatible presets (base_url/model_id remain independent)
# ---------------------------------------------------------------------------


def _preset(pid: str, base_url: str, *, keyless: bool, protocol: str) -> ProviderProfile:
    return ProviderProfile(
        id=pid,
        protocol=protocol,
        base_url=base_url,
        auth_mode=AuthMode.KEYLESS if keyless else AuthMode.OPTIONAL_KEY,
        capabilities=frozenset({"streaming", "tools"}),
        cache_mode=CacheMode.NONE,
        timeouts={
            "connect": 5.0,
            "first_event": 300.0,
            "idle": 600.0,
            "total": 3600.0,
        },  # local-model friendly
        discovery_mode=DiscoveryMode.MANUAL,
        compatibility_profile="local",
    )


PRESETS: dict[str, ProviderProfile] = {
    # The deterministic test/local adapter participates in the same
    # provenance chain as network providers; it simply has no wire endpoint.
    "fake": ProviderProfile(
        id="fake",
        protocol=Protocol.OPENAI_COMPAT,
        base_url="",
        auth_mode=AuthMode.KEYLESS,
        cache_mode=CacheMode.NONE,
        compatibility_profile="test",
    ),
    "ollama": _preset(
        "ollama", "http://127.0.0.1:11434/v1", keyless=True, protocol=Protocol.OPENAI_COMPAT
    ),
    "lmstudio": _preset(
        "lmstudio", "http://127.0.0.1:1234/v1", keyless=True, protocol=Protocol.OPENAI_COMPAT
    ),
    "vllm": _preset(
        "vllm", "http://127.0.0.1:8000/v1", keyless=True, protocol=Protocol.OPENAI_COMPAT
    ),
    "llamacpp": _preset(
        "llamacpp", "http://127.0.0.1:8080/v1", keyless=True, protocol=Protocol.OPENAI_COMPAT
    ),
    "openai-compat": _preset("openai-compat", "", keyless=False, protocol=Protocol.OPENAI_COMPAT),
    "openai": ProviderProfile(
        id="openai",
        protocol=Protocol.OPENAI,
        base_url="https://api.openai.com/v1",
        auth_mode=AuthMode.REQUIRED_KEY,
        api_key_ref="OPENAI_API_KEY",
        capabilities=frozenset({"streaming", "tools", "parallel_tools", "vision", "reasoning"}),
        cache_mode=CacheMode.AUTOMATIC_PREFIX,
        compatibility_profile="hosted",
    ),
    "anthropic": ProviderProfile(
        id="anthropic",
        protocol=Protocol.ANTHROPIC,
        base_url="https://api.anthropic.com",
        auth_mode=AuthMode.REQUIRED_KEY,
        api_key_ref="ANTHROPIC_API_KEY",
        capabilities=frozenset({"streaming", "tools", "vision", "reasoning"}),
        cache_mode=CacheMode.SESSION_KEY,
        compatibility_profile="hosted",
    ),
}


def resolve_profile(
    kind_or_id: str, *, base_url: str | None = None, model_id: str | None = None
) -> ProviderProfile:
    """Resolve a preset by kind, optionally overriding endpoint/model.

    Discovery failure never erases manual configuration: an explicit
    base_url/model_id always wins over the preset default.

    P2-65: an unknown provider id is REJECTED rather than silently falling
    back to the OpenAI hosted preset (which would send traffic to
    api.openai.com unexpectedly). Generic OpenAI-compatible endpoints must
    be explicit.
    """
    preset = PRESETS.get(kind_or_id)
    if preset is None:
        known = ", ".join(sorted(PRESETS))
        raise ValueError(
            f"unknown provider profile {kind_or_id!r}; known: {known}. "
            "For a custom OpenAI-compatible endpoint use kind='openai-compat' "
            "with an explicit base_url."
        )
    overrides: dict[str, Any] = {}
    if base_url:
        overrides["base_url"] = base_url
    if model_id:
        overrides["model_id"] = model_id
    return replace(preset, **overrides) if overrides else preset


# ---------------------------------------------------------------------------
# Compatibility candidate discovery (observed quirks -> reviewed promotion)
# ---------------------------------------------------------------------------


class CompatibilityCandidates:
    """Telemetry-driven alias/coercion suggestions. NEVER auto-promotes.

    Observed failures accumulate per (model_pattern, capability, rule);
    a human or test suite promotes them into a versioned profile change.
    """

    def __init__(self) -> None:
        self._observed: dict[str, dict] = {}

    def record_failure(self, *, model: str, capability: str, rule: str, detail: str = "") -> None:
        key = f"{model}|{capability}|{rule}"
        entry = self._observed.setdefault(
            key,
            {
                "model_pattern": model,
                "capability": capability,
                "rule": rule,
                "count": 0,
                "ambiguity": 0,
                "examples": [],
            },
        )
        entry["count"] += 1
        if len(entry["examples"]) < 3 and detail:
            entry["examples"].append(detail)

    def proposals(self, min_count: int = 10) -> list[dict]:
        return [
            dict(v, suggested=True)
            for v in self._observed.values()
            if v["count"] >= min_count and v["ambiguity"] == 0
        ]
