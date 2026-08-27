from __future__ import annotations

import json

import pytest

from athena.models.providers.anthropic import AnthropicProvider
from athena.protocol.messages import (
    CapabilityCallBlock,
    ImageBlock,
    Message,
    ReasoningBlock,
    Role,
    TextBlock,
)
from athena.protocol.models import (
    ModelEventType,
    ModelRequest,
    ModelResponseAccumulator,
)


class _SSE:
    def __init__(self, payloads):
        self._lines = ["data: " + json.dumps(payload) for payload in payloads]

    async def aiter_lines(self):
        for line in self._lines:
            yield line


def _request() -> ModelRequest:
    return ModelRequest(
        messages=(), model="claude-test", provider="anthropic",
        request_id="request-1",
        metadata={
            "provider_profile_id": "anthropic-hosted",
            "provider_profile_fingerprint": "fp-anthropic",
            "protocol": "anthropic",
        },
    )


def test_anthropic_translation_preserves_image_parts():
    provider = AnthropicProvider(api_key="key", use_sdk=False)
    message = Message(
        id="media", role=Role.USER,
        blocks=(ImageBlock(data_path="https://example.test/image.png"),),
        created_at=None, provenance=None,
    )

    translated = provider._translate_messages(
        ModelRequest(
            messages=(message,), model="claude-test", provider="anthropic",
            request_id="request-media",
        )
    )
    assert translated[0]["content"] == [{
        "type": "image",
        "source": {"type": "url", "url": "https://example.test/image.png"},
    }]


@pytest.mark.asyncio
async def test_anthropic_stream_accumulates_reasoning_text_and_tool_call():
    provider = AnthropicProvider(api_key="key", use_sdk=False)
    request = _request()
    response_events = [
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": "inspect first"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": " carefully"}},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "text_delta", "text": "I will inspect it."}},
        {"type": "content_block_start", "index": 2,
         "content_block": {"type": "tool_use", "id": "tool-1", "name": "fs.read"}},
        {"type": "content_block_delta", "index": 2,
         "delta": {"type": "input_json_delta", "partial_json": '{"path":"README.md"}'}},
        {"type": "content_block_stop", "index": 2},
        {"type": "message_stop"},
    ]

    events = [e async for e in provider._iter_stream(request, _SSE(response_events))]
    accumulator = ModelResponseAccumulator(request)
    for event in events:
        accumulator.ingest(event)

    response = accumulator.finish()
    assert [type(block) for block in response.blocks] == [
        ReasoningBlock, TextBlock, CapabilityCallBlock,
    ]
    assert response.blocks[0].text == "inspect first carefully"
    assert response.blocks[1].text == "I will inspect it."
    assert response.blocks[2].candidate is not None
    assert response.blocks[2].arguments == {"path": "README.md"}
    assert any(event.type is ModelEventType.REASONING for event in events)
    done = next(event.response for event in events if event.response is not None)
    assert done.metadata["provider_profile_id"] == "anthropic-hosted"
    assert done.metadata["provider_profile_fingerprint"] == "fp-anthropic"


def test_anthropic_malformed_tool_input_keeps_empty_raw_candidate():
    provider = AnthropicProvider(api_key="key", use_sdk=False)
    event = provider._parse_complete(
        _request(),
        {"content": [{"type": "tool_use", "id": "tool-empty",
                       "name": "fs.read", "input": None}]},
    )
    block = event.response.blocks[0]
    assert isinstance(block, CapabilityCallBlock)
    assert block.arguments == {}
    assert block.candidate is not None
    assert block.candidate.raw_arguments == ""
    assert block.candidate.parsed_arguments is None
