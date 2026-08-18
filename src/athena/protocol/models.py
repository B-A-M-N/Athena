"""Model provider abstraction.

Every model backend normalizes to the ModelProvider protocol (BUILDSPEC
sections 23-27). Streaming is the canonical API; a consumer that wants a
complete response accumulates streamed events. Provider adapters own provider
retries. The kernel MUST NOT blanket-retry.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping, Protocol, Sequence

from athena.protocol.capabilities import CapabilityDescriptor
from athena.protocol.messages import ContentBlock, Message


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


@dataclass(frozen=True)
class ModelResponse:
    request_id: str
    model: str
    provider: str
    blocks: tuple[ContentBlock, ...] = ()
    finish_reason: str | None = "stop"
    usage: UsageInfo = UsageInfo()


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


class ModelProvider(Protocol):
    async def list_models(self) -> Sequence[ModelInfo]:
        ...

    def complete(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        ...

    async def cancel(self, request_id: str) -> None:
        ...


__all__ = [
    "PrivacyClass", "CostInfo", "ModelInfo", "ModelRequest", "UsageInfo",
    "ModelResponse", "ModelDelta", "ModelEvent", "ModelEventType",
    "ModelProvider",
]