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
from athena.protocol.messages import (
    CapabilityCallBlock,
    ContentBlock,
    Message,
    ReasoningBlock,
    TextBlock,
)


class PrivacyClass(str, enum.Enum):
    LOCAL = "local"
    REMOTE = "remote"
    HYBRID = "hybrid"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CostInfo:
    per_1m_input: float | None = None
    per_1m_output: float | None = None
    currency: str = "USD"


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


class ModelResponseAccumulator:
    """Canonical mixed-content assembly for a provider stream.

    Providers may expose text/reasoning as deltas and tool calls as completed
    blocks, while also sending a final response that contains only some of
    those parts.  This object is the single boundary that combines them.  A
    caller must not independently choose between streamed blocks and the
    provider's DONE response.

    Text and reasoning deltas are authoritative when present: a provider's
    final response commonly repeats them, and appending both would duplicate
    content.  Completed blocks are merged by stable call identity.
    """

    def __init__(self, request: ModelRequest) -> None:
        self._request = request
        self._text: list[str] = []
        self._reasoning: list[str] = []
        self._blocks: list[ContentBlock] = []
        self._response: ModelResponse | None = None

    @property
    def has_response(self) -> bool:
        """Whether the provider emitted a terminal response event."""
        return self._response is not None

    def ingest(self, event: ModelEvent) -> None:
        if event.delta is not None:
            if event.delta.text:
                self._text.append(event.delta.text)
            if event.delta.reasoning:
                self._reasoning.append(event.delta.reasoning)
            if event.delta.block is not None:
                self._blocks.append(event.delta.block)
        if event.response is not None:
            self._response = event.response

    def finish(self) -> ModelResponse:
        response = self._response
        if response is None:
            response = ModelResponse(
                request_id=self._request.request_id,
                model=self._request.model,
                provider=self._request.provider,
                blocks=(),
            )

        streamed_text = "".join(self._text)
        streamed_reasoning = "".join(self._reasoning)
        base = list(response.blocks)

        if streamed_reasoning:
            base = _replace_or_prepend(
                base, ReasoningBlock(type="reasoning", text=streamed_reasoning),
                ReasoningBlock,
            )
        if streamed_text:
            base = _replace_or_prepend(
                base, TextBlock(type="text", text=streamed_text), TextBlock,
            )

        identities = {_content_identity(block) for block in base}
        for block in self._blocks:
            if _content_identity(block) not in identities:
                base.append(block)
                identities.add(_content_identity(block))

        return ModelResponse(
            request_id=response.request_id or self._request.request_id,
            model=response.model or self._request.model,
            provider=response.provider or self._request.provider,
            blocks=tuple(base),
            finish_reason=response.finish_reason,
            usage=response.usage,
            metadata=response.metadata,
        )


def _replace_or_prepend(
    blocks: list[ContentBlock], replacement: ContentBlock, block_type: type,
) -> list[ContentBlock]:
    for index, block in enumerate(blocks):
        if isinstance(block, block_type):
            blocks[index] = replacement
            return blocks
    # Text/reasoning precedes tool calls in the canonical message shape.
    insert_at = next(
        (index for index, block in enumerate(blocks)
         if isinstance(block, CapabilityCallBlock)),
        len(blocks),
    )
    blocks.insert(insert_at, replacement)
    return blocks


def _content_identity(block: ContentBlock) -> tuple[str, str]:
    if isinstance(block, CapabilityCallBlock):
        return ("capability_call", block.call_id or repr(block))
    return (type(block).__name__, repr(block))


class ModelProvider(Protocol):
    async def list_models(self) -> Sequence[ModelInfo]:
        ...

    def complete(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        ...

    async def cancel(self, request_id: str) -> None:
        ...


__all__ = [
    "PrivacyClass", "CostInfo", "ModelInfo", "ModelRequest", "UsageInfo",
    "ToolCallCandidate",
    "ModelResponse", "ModelDelta", "ModelEvent", "ModelEventType",
    "ModelResponseAccumulator",
    "ModelProvider",
]
