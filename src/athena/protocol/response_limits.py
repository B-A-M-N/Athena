"""Provider-stream payload limits and canonical mixed-content helpers."""

from __future__ import annotations

import json

from athena.protocol.messages import (
    CapabilityCallBlock,
    ContentBlock,
    ReasoningBlock,
    TextBlock,
)


class StreamOutputLimitExceeded(ValueError):
    """The provider emitted more output than the local request envelope allows."""

    def __init__(self, *, estimated_tokens: int, raw_bytes: int, token_limit: int, byte_limit: int):
        super().__init__("provider stream exceeded the local output limit")
        self.estimated_tokens = estimated_tokens
        self.raw_bytes = raw_bytes
        self.token_limit = token_limit
        self.byte_limit = byte_limit


def merge_streamed_content(
    blocks: list[ContentBlock], streamed: str, block_type: type[ContentBlock]
) -> list[ContentBlock]:
    """Merge deltas without flattening independently ordered final blocks."""
    if not streamed:
        return blocks
    final_text = "".join(block.text for block in blocks if isinstance(block, block_type))
    if final_text == streamed:
        return blocks
    replacement = block_type(text=streamed)
    for index, block in enumerate(blocks):
        if isinstance(block, block_type):
            if sum(isinstance(item, block_type) for item in blocks) == 1:
                blocks[index] = replacement
            return blocks
    insert_at = next(
        (index for index, block in enumerate(blocks) if isinstance(block, CapabilityCallBlock)),
        len(blocks),
    )
    blocks.insert(insert_at, replacement)
    return blocks


def block_payload(block: ContentBlock) -> str:
    """Bound payload accounting for every streamed block family."""
    if isinstance(block, (TextBlock, ReasoningBlock)):
        return block.text
    if isinstance(block, CapabilityCallBlock):
        return json.dumps(
            {
                "call_id": block.call_id,
                "capability_id": block.capability_id,
                "arguments": dict(block.arguments or {}),
                "candidate": block.candidate,
            },
            sort_keys=True,
            default=str,
        )
    return repr(block)


def content_identity(block: ContentBlock) -> tuple[str, str]:
    if isinstance(block, CapabilityCallBlock):
        return ("capability_call", block.call_id or repr(block))
    return (type(block).__name__, repr(block))


__all__ = [
    "StreamOutputLimitExceeded",
    "block_payload",
    "content_identity",
    "merge_streamed_content",
]
