"""Canonical mixed-content assembly for provider response streams."""

from __future__ import annotations

from typing import Any

from athena.protocol.messages import CapabilityCallBlock, ContentBlock, ReasoningBlock, TextBlock
from athena.protocol.models import (
    IncompleteModelResponse,
    ModelDelta,
    ModelEvent,
    ModelEventType,
    ModelRequest,
    ModelResponse,
)
from athena.protocol.response_limits import (
    StreamOutputLimitExceeded,
    block_payload,
    content_identity,
    merge_streamed_content,
)


class ModelResponseAccumulator:
    """Assemble deltas and terminal provider content exactly once."""

    def __init__(self, request: ModelRequest) -> None:
        self._request = request
        self._text: list[str] = []
        self._reasoning: list[str] = []
        self._blocks: list[ContentBlock] = []
        self._response: ModelResponse | None = None
        self._terminal_event_seen = False
        self._stream_tokens = 0
        self._stream_bytes = 0
        self._stream_token_limit = max(1, int(request.max_tokens or 65_536))
        self._stream_byte_limit = max(4 * 1024 * 1024, self._stream_token_limit * 16)

    @property
    def has_response(self) -> bool:
        return self._terminal_event_seen and self._response is not None

    @property
    def has_partial_output(self) -> bool:
        return bool(self._text or self._reasoning or self._blocks)

    @property
    def stream_usage(self) -> tuple[int, int]:
        return self._stream_tokens, self._stream_bytes

    def ingest(self, event: ModelEvent) -> None:
        if self._terminal_event_seen:
            return
        if event.delta is not None:
            self._account_delta(event.delta)
            if event.delta.text:
                self._text.append(event.delta.text)
            if event.delta.reasoning:
                self._reasoning.append(event.delta.reasoning)
            if event.delta.block is not None:
                self._blocks.append(event.delta.block)
        if event.type is ModelEventType.DONE and event.response is not None:
            self._account_terminal_additions(event.response)
            self._response = event.response
            self._terminal_event_seen = True

    def _account_delta(self, delta: ModelDelta) -> None:
        payloads: list[str] = []
        if delta.text:
            payloads.append(delta.text)
        if delta.reasoning:
            payloads.append(delta.reasoning)
        if delta.block is not None:
            payloads.append(block_payload(delta.block))
        self._account_payload("".join(payloads))

    def _account_terminal_additions(self, response: ModelResponse) -> None:
        """Account terminal payload not already observed in stream deltas.

        Providers commonly repeat the streamed text, reasoning, and tool-call
        blocks in their terminal response.  Counting those duplicates would
        reject valid responses at the local limit, while ignoring genuinely
        new terminal blocks would allow an oversized response through.
        """
        for block_type, parts in (
            (TextBlock, list(self._text)),
            (ReasoningBlock, list(self._reasoning)),
        ):
            terminal = [block for block in response.blocks if isinstance(block, block_type)]
            if not terminal:
                continue
            if "".join(block.text for block in terminal) == "".join(parts):
                continue
            remaining = list(parts)
            for block in terminal:
                try:
                    remaining.remove(block.text)
                except ValueError:
                    self._account_payload(block.text)

        streamed_calls = {
            content_identity(block)
            for block in self._blocks
            if isinstance(block, CapabilityCallBlock)
        }
        for terminal_block in response.blocks:
            if isinstance(terminal_block, (TextBlock, ReasoningBlock)):
                continue
            if (
                isinstance(terminal_block, CapabilityCallBlock)
                and content_identity(terminal_block) in streamed_calls
            ):
                continue
            self._account_payload(block_payload(terminal_block))

    def _account_payload(self, payload: str) -> None:
        raw_bytes = len(payload.encode("utf-8", errors="replace"))
        estimated_tokens = max(0, (len(payload) + 3) // 4)
        next_bytes = self._stream_bytes + raw_bytes
        next_tokens = self._stream_tokens + estimated_tokens
        if next_bytes > self._stream_byte_limit or next_tokens > self._stream_token_limit:
            raise StreamOutputLimitExceeded(
                estimated_tokens=next_tokens,
                raw_bytes=next_bytes,
                token_limit=self._stream_token_limit,
                byte_limit=self._stream_byte_limit,
            )
        self._stream_bytes = next_bytes
        self._stream_tokens = next_tokens

    def finish(self) -> ModelResponse:
        if not self.has_response:
            partial = self._assemble(
                ModelResponse(
                    request_id=self._request.request_id,
                    model=self._request.model,
                    provider=self._request.provider,
                    blocks=(),
                    finish_reason=None,
                    metadata={"stream_status": "incomplete"},
                )
            )
            raise IncompleteModelResponse(partial, self.diagnostic())
        assert self._response is not None
        return self._assemble(self._response)

    def diagnostic(self) -> dict[str, Any]:
        partial = self._assemble(
            ModelResponse(
                request_id=self._request.request_id,
                model=self._request.model,
                provider=self._request.provider,
                blocks=(),
                finish_reason=None,
                metadata={"stream_status": "incomplete"},
            )
        )
        text = "".join(block.text for block in partial.blocks if isinstance(block, TextBlock))
        reasoning = "".join(
            block.text for block in partial.blocks if isinstance(block, ReasoningBlock)
        )
        return {
            "status": "incomplete",
            "request_id": partial.request_id,
            "model": partial.model,
            "provider": partial.provider,
            "text": text[:12000],
            "reasoning": reasoning[:12000],
            "blocks": [repr(block)[:2000] for block in partial.blocks],
        }

    def _assemble(self, response: ModelResponse) -> ModelResponse:
        streamed_text = "".join(self._text)
        streamed_reasoning = "".join(self._reasoning)
        base = list(response.blocks)
        base = merge_streamed_content(
            base,
            streamed_reasoning,
            ReasoningBlock,
            streamed_parts=tuple(self._reasoning),
        )
        base = merge_streamed_content(
            base,
            streamed_text,
            TextBlock,
            streamed_parts=tuple(self._text),
        )
        identities = {content_identity(block) for block in base}
        for block in self._blocks:
            if content_identity(block) not in identities:
                base.append(block)
                identities.add(content_identity(block))
        return ModelResponse(
            request_id=response.request_id or self._request.request_id,
            model=response.model or self._request.model,
            provider=response.provider or self._request.provider,
            blocks=tuple(base),
            finish_reason=response.finish_reason,
            usage=response.usage,
            metadata=response.metadata,
        )


__all__ = ["ModelResponseAccumulator"]
