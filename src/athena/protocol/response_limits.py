"""Provider-stream payload limits and canonical mixed-content helpers."""

from __future__ import annotations

import json
from typing import TypeVar

from athena.protocol.messages import (
    CapabilityCallBlock,
    ContentBlock,
    ReasoningBlock,
    TextBlock,
)

_TextualBlock = TypeVar("_TextualBlock", TextBlock, ReasoningBlock)


class StreamOutputLimitExceeded(ValueError):
    """The provider emitted more output than the local request envelope allows."""

    def __init__(self, *, estimated_tokens: int, raw_bytes: int, token_limit: int, byte_limit: int):
        super().__init__("provider stream exceeded the local output limit")
        self.estimated_tokens = estimated_tokens
        self.raw_bytes = raw_bytes
        self.token_limit = token_limit
        self.byte_limit = byte_limit


def merge_streamed_content(
    blocks: list[ContentBlock],
    streamed: str,
    block_type: type[_TextualBlock],
    *,
    streamed_parts: tuple[str, ...] | list[str] = (),
) -> list[ContentBlock]:
    """Merge deltas without flattening independently ordered final blocks.

    Terminal provider responses can split one logical text stream around tool
    calls, while some adapters return one terminal text block.  Exact
    concatenation is the strongest duplicate proof; otherwise match streamed
    chunks by exact block content and insert only unmatched chunks.  This
    keeps independent terminal blocks and their ordering intact.
    """
    if not streamed:
        return blocks
    final_text = "".join(block.text for block in blocks if isinstance(block, block_type))
    if final_text == streamed:
        return blocks
    parts = [part for part in streamed_parts if part] or [streamed]
    cursor = 0
    merged: list[ContentBlock] = []
    saw_terminal_block = False
    for block in blocks:
        if not isinstance(block, block_type):
            merged.append(block)
            continue
        saw_terminal_block = True
        match = next(
            (index for index in range(cursor, len(parts)) if parts[index] == block.text),
            None,
        )
        if match is not None:
            merged.extend(block_type(text=part) for part in parts[cursor:match])
            cursor = match + 1
        merged.append(block)
    if not saw_terminal_block:
        insert_at = next(
            (
                index
                for index, block in enumerate(merged)
                if isinstance(block, CapabilityCallBlock)
            ),
            len(merged),
        )
        merged[insert_at:insert_at] = [block_type(text=part) for part in parts]
        return merged
    merged.extend(block_type(text=part) for part in parts[cursor:])
    return merged


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
