"""Prefix-cache coordinator and replay fidelity envelopes.

Central insight (adopted verbatim): caching is an end-to-end session
invariant, not a provider header toggle. A stable prefix must remain stable
across the WHOLE path — system prompt, tool definitions, tool order, schema
serialization, role mapping — with history append-only between explicit
boundaries.

Components:
    PromptEnvelope     partitions compiled context into STABLE PREFIX /
                       APPEND-ONLY HISTORY / DYNAMIC SUFFIX.
    PrefixTracker      fingerprints the prefix, detects drift, records the
                       first changed component as the invalidation reason.
    CacheBoundary      durable invalidation event values.
    UsageRecord        normalized cross-provider cache accounting.
    InferenceReceipt   provider-native replay metadata that survives
                       canonicalization for same-model continuation.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = ["CacheBoundary", "PromptEnvelope", "PrefixTracker", "UsageRecord",
           "InferenceReceipt"]


class CacheBoundary:
    SYSTEM_PROMPT_CHANGED = "system_prompt_changed"
    TOOLS_CHANGED = "tools_changed"
    TOOL_SCHEMA_CHANGED = "tool_schema_changed"
    MODEL_CHANGED = "model_changed"
    PROVIDER_PROFILE_CHANGED = "provider_profile_changed"
    ROLE_MAPPING_CHANGED = "role_mapping_changed"
    COMPACTION_APPLIED = "compaction_applied"
    COMPATIBILITY_POLICY_CHANGED = "compatibility_policy_changed"
    MANUAL_RESET = "manual_reset"


def _fp(obj: Any) -> str:
    return hashlib.sha256(json.dumps(
        obj, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()[:16]


@dataclass
class PromptEnvelope:
    """Three-zone partition of one compiled request."""

    stable_prefix: list[Any] = field(default_factory=list)     # system/identity/schemas/policies
    append_history: list[Any] = field(default_factory=list)    # assistant/tool-result turns
    dynamic_suffix: list[Any] = field(default_factory=list)    # latest observations/current request

    def fingerprint(self) -> dict:
        return {
            "prefix": _fp(self.stable_prefix),
            "history": _fp(self.append_history),
            "suffix": _fp(self.dynamic_suffix),
        }


class PrefixTracker:
    """Tracks prefix stability across requests of one session."""

    def __init__(self) -> None:
        self.last_prefix_fp: str | None = None
        self.last_full_fp: str | None = None
        self.boundaries: list[dict] = []
        self.components_fp: dict[str, str] = {}

    def observe(self, envelope: PromptEnvelope,
                components: dict[str, Any] | None = None) -> dict:
        """Observe one request. Returns {stable, boundary?, first_change?}.

        ``components`` maps prefix-defining component names (system_prompt,
        tools, model...) to their current content so a drift can be
        attributed to the first changed component.
        """
        fps = envelope.fingerprint()
        outcome: dict[str, Any] = {"prefix_fp": fps["prefix"],
                                   "stable": False}
        if components:
            # Always track component fingerprints so any later drift is
            # attributable, even if this observation was already stable.
            new_c = {k: _fp(v) for k, v in components.items()}
        else:
            new_c = None

        if self.last_prefix_fp is not None and self.last_prefix_fp != fps["prefix"]:
            reason = "unknown"
            if new_c and self.components_fp:
                for key, old in self.components_fp.items():
                    if new_c.get(key) != old:
                        reason = f"{key}_changed"
                        break
            elif not new_c:
                reason = CacheBoundary.TOOL_SCHEMA_CHANGED  # conservative
            boundary = {
                "reason": reason,
                "old_prefix": self.last_prefix_fp,
                "new_prefix": fps["prefix"],
                "ts": time.time(),
            }
            self.boundaries.append(boundary)
            outcome["boundary"] = boundary
        else:
            outcome["stable"] = True

        if new_c:
            self.components_fp.update(new_c)
        self.last_prefix_fp = fps["prefix"]
        self.last_full_fp = _fp([fps["prefix"], fps["history"], fps["suffix"]])
        return outcome


class UsageRecord:
    """Normalized cache accounting across providers."""

    @staticmethod
    def from_openai_compat(usage: Mapping[str, Any]) -> "UsageRecord":
        details = usage.get("prompt_tokens_details") or {}
        cached = int(details.get("cached_tokens") or 0)
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        return UsageRecord(
            prompt_tokens=prompt, completion_tokens=completion,
            cache_read_tokens=cached,
            uncached_prompt_tokens=max(prompt - cached, 0))

    @staticmethod
    def from_anthropic(usage: Mapping[str, Any]) -> "UsageRecord":
        read = int((usage.get("cache_read_input_tokens")) or 0)
        write = int((usage.get("cache_creation_input_tokens")) or 0)
        prompt = int(usage.get("input_tokens") or 0)
        completion = int(usage.get("output_tokens") or 0)
        return UsageRecord(
            prompt_tokens=prompt + read + write,
            completion_tokens=completion,
            cache_read_tokens=read, cache_write_tokens=write,
            uncached_prompt_tokens=prompt)

    def __init__(self, *, prompt_tokens: int = 0, completion_tokens: int = 0,
                 cache_read_tokens: int = 0, cache_write_tokens: int = 0,
                 uncached_prompt_tokens: int | None = None) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.cache_read_tokens = cache_read_tokens
        self.cache_write_tokens = cache_write_tokens
        self.uncached_prompt_tokens = (
            uncached_prompt_tokens if uncached_prompt_tokens is not None
            else max(prompt_tokens - cache_read_tokens - cache_write_tokens, 0))

    @property
    def cache_rate(self) -> float | None:
        if self.prompt_tokens <= 0:
            return None
        return self.cache_read_tokens / self.prompt_tokens

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "uncached_prompt_tokens": self.uncached_prompt_tokens,
            "cache_rate": round(self.cache_rate, 4) if self.cache_rate is not None else None,
        }


@dataclass
class InferenceReceipt:
    """Provider-native replay state that MUST survive canonicalization.

    Persisted per assistant message so 'resume task' truly resumes the
    model conversation instead of reconstructing a superficially similar
    prompt and hoping the provider accepts it.
    """

    call_id: str
    provider_profile_id: str
    model_id: str
    response_id: str | None = None
    reasoning_signature: str | None = None       # anthropic thought signature
    encrypted_reasoning: bytes | str | None = None
    tool_ids: tuple[str, ...] = ()
    continuation_token: str | None = None
    provider_metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "call_id": self.call_id,
            "provider_profile_id": self.provider_profile_id,
            "model_id": self.model_id,
            "response_id": self.response_id,
            "tool_ids": list(self.tool_ids),
            "provider_metadata": self.provider_metadata,
        }
        # Reasoning payloads may be sensitive/large; store presence markers
        # plus the payload itself only when the provider requires it.
        if self.reasoning_signature is not None:
            d["reasoning_signature"] = self.reasoning_signature
        if self.encrypted_reasoning is not None:
            d["has_encrypted_reasoning"] = True
        if self.continuation_token is not None:
            d["continuation_token"] = self.continuation_token
        return d
