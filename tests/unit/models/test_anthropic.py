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
        messages=(),
        model="claude-test",
        provider="anthropic",
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
        id="media",
        role=Role.USER,
        blocks=(ImageBlock(data_path="https://example.test/image.png"),),
        created_at=None,
        provenance=None,
    )

    translated = provider._translate_messages(
        ModelRequest(
            messages=(message,),
            model="claude-test",
            provider="anthropic",
            request_id="request-media",
        )
    )
    assert translated[0]["content"] == [
        {
            "type": "image",
            "source": {"type": "url", "url": "https://example.test/image.png"},
        }
    ]


def test_anthropic_cache_breakpoint_covers_stable_context_before_task():
    provider = AnthropicProvider(api_key="key", use_sdk=False)
    messages = (
        Message(
            id="system",
            role=Role.SYSTEM,
            blocks=(TextBlock(text="runtime policy"),),
            created_at=None,
            provenance=None,
        ),
        Message(
            id="project",
            role=Role.USER,
            blocks=(TextBlock(text="project contract"),),
            created_at=None,
            provenance=None,
        ),
        Message(
            id="task",
            role=Role.USER,
            blocks=(TextBlock(text="current task"),),
            created_at=None,
            provenance=None,
        ),
    )
    request = ModelRequest(
        messages=messages,
        model="claude-test",
        provider="anthropic",
        request_id="request-cache",
        metadata={
            "cache_mode": "session-key",
            "cache_prefix_message_count": 2,
        },
    )

    payload = provider._build_kwargs(request, stream=False)

    assert payload["system"] == "runtime policy"
    assert payload["messages"][0]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in payload["messages"][1]["content"][-1]


@pytest.mark.asyncio
async def test_anthropic_stream_accumulates_reasoning_text_and_tool_call():
    provider = AnthropicProvider(api_key="key", use_sdk=False)
    request = _request()
    response_events = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "inspect first"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": " carefully"},
        },
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}},
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "I will inspect it."},
        },
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {"type": "tool_use", "id": "tool-1", "name": "fs.read"},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"path":"README.md"}'},
        },
        {"type": "content_block_stop", "index": 2},
        {"type": "message_stop"},
    ]

    events = [e async for e in provider._iter_stream(request, _SSE(response_events))]
    accumulator = ModelResponseAccumulator(request)
    for event in events:
        accumulator.ingest(event)

    response = accumulator.finish()
    assert [type(block) for block in response.blocks] == [
        ReasoningBlock,
        TextBlock,
        CapabilityCallBlock,
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
        {"content": [{"type": "tool_use", "id": "tool-empty", "name": "fs.read", "input": None}]},
    )
    block = event.response.blocks[0]
    assert isinstance(block, CapabilityCallBlock)
    assert block.arguments == {}
    assert block.candidate is not None
    assert block.candidate.raw_arguments == ""
    assert block.candidate.parsed_arguments is None


def test_anthropic_response_preserves_provider_reported_cost():
    provider = AnthropicProvider(api_key="key", use_sdk=False)
    event = provider._parse_complete(
        _request(),
        {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 4, "output_tokens": 2, "cost": 0.0007},
        },
    )

    assert event.response.usage.cost_usd == 0.0007
