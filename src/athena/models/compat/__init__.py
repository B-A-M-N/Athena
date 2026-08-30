"""Inference Compatibility Kernel.

One model-boundary system with three cooperating, independently testable
modules — provider/model compatibility profiles, prefix-cache coordination,
and bounded deterministic tool-input repair. Athena-native: no second agent
loop, no second execution authority. Repair happens BEFORE policy and
approval; the exact canonical arguments approved are the ones executed.
"""

from athena.models.compat.caching import (
    CacheBoundary,
    InferenceReceipt,
    PrefixTracker,
    PromptEnvelope,
    UsageRecord,
    build_cache_key,
    cache_fingerprint,
    cache_message_payload,
)
from athena.models.compat.profiles import (
    AuthMode,
    CacheMode,
    CompatibilityProfile,
    CompatibilityCandidates,
    COMPATIBILITY_PRESETS,
    DiscoveryMode,
    ModelProfile,
    PRESETS,
    Protocol,
    ProviderProfile,
    resolve_profile,
    schema_fingerprint,
    resolve_compatibility_profile,
)
from athena.models.compat.toolrepair import (
    BUILTIN_ALIASES,
    RepairOutcome,
    RepairReceipt,
    ToolInputRepairer,
)

__all__ = [
    "AuthMode",
    "build_cache_key",
    "cache_fingerprint",
    "cache_message_payload",
    "CacheBoundary",
    "CacheMode",
    "CompatibilityCandidates",
    "CompatibilityProfile",
    "DiscoveryMode",
    "InferenceReceipt",
    "COMPATIBILITY_PRESETS",
    "ModelProfile",
    "PRESETS",
    "PrefixTracker",
    "PromptEnvelope",
    "Protocol",
    "ProviderProfile",
    "BUILTIN_ALIASES",
    "RepairOutcome",
    "RepairReceipt",
    "ToolInputRepairer",
    "UsageRecord",
    "resolve_profile",
    "resolve_compatibility_profile",
    "schema_fingerprint",
]
