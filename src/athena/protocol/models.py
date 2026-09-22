"""Model provider abstraction.

Every model backend normalizes to the ModelProvider protocol (BUILDSPEC
sections 23-27). Streaming is the canonical API; a consumer that wants a
complete response accumulates streamed events. Provider adapters own provider
retries. The kernel MUST NOT blanket-retry.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping, Protocol, Sequence

from athena.protocol.capabilities import CapabilityDescriptor
from athena.protocol.messages import ContentBlock, Message


class PrivacyClass(str, enum.Enum):
    LOCAL = "local"
    REMOTE = "remote"
    HYBRID = "hybrid"
    UNKNOWN = "unknown"


class ModelQualityTier(str, enum.Enum):
    """Declared capability tier of a model (P1-16).

    Advisory routing metadata, not a verdict: providers declare where a
    model sits on the reasoning/coding/tool-use capability ladder, and
    routing uses the declaration to hold work-bearing turns to a floor.
    ``UNDECLARED`` is its own tier — a model with no declaration is
    never excluded by a floor it could not have known about; the floor
    only binds declared models.
    """

    ECONOMY = "economy"  # cheap conversational / high-volume utility tier
    STANDARD = "standard"  # ordinary work tier
    FRONTIER = "frontier"  # strongest reasoning / coding / agentic tier
    UNDECLARED = "undeclared"

    @property
    def rank(self) -> int:
        return _TIER_RANK.get(self, -1)


_TIER_RANK: dict[ModelQualityTier, int] = {
    ModelQualityTier.UNDECLARED: 0,
    ModelQualityTier.ECONOMY: 1,
    ModelQualityTier.STANDARD: 2,
    ModelQualityTier.FRONTIER: 3,
}


@dataclass(frozen=True)
class CostInfo:
    per_1m_input: float | None = None
    per_1m_output: float | None = None
    currency: str = "USD"
    per_1m_cache_read_input: float | None = None
    per_1m_cache_write_input: float | None = None


@dataclass(frozen=True)
class ModelRequirements:
    """Neutral hard requirements shared by context compilation and routing."""

    required_capabilities: frozenset[str] = frozenset()
    estimated_input_tokens: int = 0
    minimum_context_window_tokens: int | None = None
    requested_output_tokens: int | None = None

    def __post_init__(self) -> None:
        estimated = int(self.estimated_input_tokens)
        if estimated < 0:
            raise ValueError("estimated_input_tokens must be non-negative")
        object.__setattr__(self, "estimated_input_tokens", estimated)
        for name in ("minimum_context_window_tokens", "requested_output_tokens"):
            value = getattr(self, name)
            if value is not None and int(value) < 0:
                raise ValueError(f"{name} must be non-negative")
            if value is not None:
                object.__setattr__(self, name, int(value))

    @property
    def needs_tools(self) -> bool:
        """Derived compatibility read of the canonical capability set."""
        return "tools" in self.required_capabilities

    @property
    def vision(self) -> bool:
        return "vision" in self.required_capabilities

    @property
    def audio(self) -> bool:
        return "audio_input" in self.required_capabilities

    @property
    def reasoning(self) -> bool:
        return "reasoning" in self.required_capabilities


@dataclass(frozen=True)
class ModelInfo:
    id: str
    provider: str
    context_limit: int | None = None
    max_output_tokens: int | None = None
    tool_calling: bool = False
    vision: bool = False
    audio_input: bool = False
    audio_output: bool = False
    reasoning: bool = False
    structured_output: bool = False
    streaming: bool = True
    cost: CostInfo | None = None
    latency_class: str | None = None
    privacy_class: PrivacyClass = PrivacyClass.UNKNOWN
    # Declared capability tier (P1-16). Advisory routing metadata; see
    # ModelQualityTier. Accepts the bare string value for convenience.
    quality_tier: ModelQualityTier = ModelQualityTier.UNDECLARED

    def __post_init__(self) -> None:
        if isinstance(self.quality_tier, str):
            try:
                object.__setattr__(self, "quality_tier", ModelQualityTier(self.quality_tier))
            except ValueError:
                object.__setattr__(self, "quality_tier", ModelQualityTier.UNDECLARED)


@dataclass(frozen=True)
class ToolCallCandidate:
    """Lossless provider boundary for a model-produced tool call.

    Providers may stream arguments as incomplete or malformed JSON.  The
    candidate keeps those bytes intact until the compatibility repair boundary
    decides whether the call can become a canonical capability request.
    """

    call_id: str
    capability_id: str
    raw_arguments: str
    parsed_arguments: dict[str, Any] | None = None
    completion_state: str = "CLEAN"
    provider_profile_id: str | None = None
    model_id: str | None = None
    provider_metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(
        cls,
        call_id: str,
        capability_id: str,
        raw: str,
        *,
        completion_state: str = "CLEAN",
        provider_profile_id: str | None = None,
        model_id: str | None = None,
        **metadata: Any,
    ) -> "ToolCallCandidate":
        parsed: dict[str, Any] | None = None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            value = None
        if isinstance(value, dict):
            parsed = value
        return cls(
            call_id=call_id,
            capability_id=capability_id,
            raw_arguments=raw,
            parsed_arguments=parsed,
            completion_state=completion_state,
            provider_profile_id=provider_profile_id,
            model_id=model_id,
            provider_metadata=dict(metadata),
        )


class LogprobsNone:
    pass


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[Message, ...]
    model: str
    provider: str
    request_id: str
    system: str | None = None
    capabilities: tuple[CapabilityDescriptor, ...] = ()
    temperature: float | None = None
    max_tokens: int | None = None
    stop: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UsageInfo:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float | None = None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    uncached_input_tokens: int | None = None
    provider_metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    request_id: str
    model: str
    provider: str
    blocks: tuple[ContentBlock, ...] = ()
    finish_reason: str | None = "stop"
    usage: UsageInfo = UsageInfo()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelDelta:
    request_id: str
    text: str = ""
    reasoning: str | None = None
    block: ContentBlock | None = None


class ModelEventType(str, enum.Enum):
    DELTA = "delta"
    REASONING = "reasoning"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class ModelEvent:
    type: ModelEventType
    request_id: str
    delta: ModelDelta | None = None
    response: ModelResponse | None = None
    error: str | None = None
    code: str | None = None


class IncompleteModelResponse(ValueError):
    """A provider stream ended before a terminal response event."""

    def __init__(self, partial_response: ModelResponse, diagnostic: Mapping[str, Any]):
        super().__init__("provider stream ended without a terminal response")
        self.partial_response = partial_response
        self.diagnostic = dict(diagnostic)


class ModelProvider(Protocol):
    async def list_models(self) -> Sequence[ModelInfo]: ...

    def complete(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...

    async def cancel(self, request_id: str) -> None: ...


from athena.protocol.response_accumulator import ModelResponseAccumulator
from athena.protocol.response_limits import StreamOutputLimitExceeded


__all__ = [
    "PrivacyClass",
    "CostInfo",
    "ModelInfo",
    "ModelRequest",
    "UsageInfo",
    "ToolCallCandidate",
    "ModelResponse",
    "ModelDelta",
    "ModelEvent",
    "ModelEventType",
    "IncompleteModelResponse",
    "StreamOutputLimitExceeded",
    "ModelResponseAccumulator",
    "ModelProvider",
]
