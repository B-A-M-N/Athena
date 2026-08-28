from __future__ import annotations

from types import SimpleNamespace

import pytest

from athena.kernel.kernel import AgentKernel, _assistant_message
from athena.models.providers.anthropic import AnthropicProvider
from athena.models.providers.openai_compat import OpenAICompatProvider
from athena.protocol.messages import (
    CapabilityCallBlock,
    CapabilityResultBlock,
    Message,
    ReasoningBlock,
    Role,
    TextBlock,
)
from athena.protocol.models import (
    ModelDelta,
    ModelEvent,
    ModelEventType,
    ModelRequest,
    ModelResponse,
)
from athena.protocol.tasks import TaskSpec


def _response():
    return ModelResponse(
        request_id="turn-1",
        model="model-1",
        provider="provider-1",
        blocks=(
            ReasoningBlock(text="I need to inspect the file."),
            TextBlock(text="I'll inspect that."),
            CapabilityCallBlock(
                call_id="tool-1",
                capability_id="fs.read",
                arguments={"path": "README.md"},
            ),
        ),
    )


def test_assistant_message_preserves_reasoning_text_and_calls():
    message = _assistant_message(
        TaskSpec(id="task-1", objective="inspect", session_id="session-1"),
        _response(),
    )
    assert [type(block) for block in message.blocks] == [
        ReasoningBlock,
        TextBlock,
        CapabilityCallBlock,
    ]
    assert message.blocks[-1].call_id == "tool-1"


@pytest.mark.athena_scenario("COMPAT-002")
def test_openai_and_anthropic_replay_preserve_mixed_assistant_turn():
    assistant = _assistant_message(
        TaskSpec(id="task-1", objective="inspect", session_id="session-1"),
        _response(),
    )
    result = Message(
        id="result-1",
        role=Role.CAPABILITY,
        blocks=(
            CapabilityResultBlock(
                call_id="tool-1",
                capability_id="fs.read",
                output="184 passed, 2 failed",
            ),
        ),
        created_at=None,
        provenance=None,
    )
    openai = OpenAICompatProvider(
        base_url="https://fake.invalid",
        api_key="key",
        model="m",
        provider="oai",
    )
    oai_assistant = openai._translate_message(assistant)
    oai_result = openai._translate_message(result)
    assert oai_assistant["content"] == "I need to inspect the file.\nI'll inspect that."
    assert oai_assistant["tool_calls"][0]["id"] == "tool-1"
    assert oai_result[0]["content"] == "184 passed, 2 failed"

    anthropic = AnthropicProvider(api_key="key", use_sdk=False)
    anthropic_messages = anthropic._translate_messages(
        ModelRequest(
            messages=(assistant, result),
            model="m",
            provider="anthropic",
            request_id="request-1",
        )
    )
    assert anthropic_messages[0]["role"] == "assistant"
    assert [part["type"] for part in anthropic_messages[0]["content"]] == [
        "text",
        "text",
        "tool_use",
    ]
    assert anthropic_messages[0]["content"][-1]["id"] == "tool-1"
    assert anthropic_messages[1]["content"][0]["tool_use_id"] == "tool-1"
    assert anthropic_messages[1]["content"][0]["content"][0]["text"] == ("184 passed, 2 failed")


@pytest.mark.asyncio
async def test_kernel_stream_assembly_keeps_text_and_tool_delta():
    class Provider:
        async def complete(self, request):
            yield ModelEvent(
                type=ModelEventType.DELTA,
                request_id=request.request_id,
                delta=ModelDelta(request_id=request.request_id, text="I'll inspect that."),
            )
            yield ModelEvent(
                type=ModelEventType.DELTA,
                request_id=request.request_id,
                delta=ModelDelta(
                    request_id=request.request_id,
                    block=CapabilityCallBlock(
                        call_id="tool-2",
                        capability_id="fs.read",
                        arguments={"path": "README.md"},
                    ),
                ),
            )
            yield ModelEvent(
                type=ModelEventType.DONE,
                request_id=request.request_id,
                response=ModelResponse(
                    request_id=request.request_id,
                    model=request.model,
                    provider=request.provider,
                    blocks=(TextBlock(text="I'll inspect that."),),
                ),
            )

    kernel = object.__new__(AgentKernel)
    kernel._model_sink = None
    kernel._token_sink = None
    kernel._events = None
    task = TaskSpec(id="task-1", objective="inspect")
    state = SimpleNamespace(
        cancel=__import__("asyncio").Event(),
        input_tokens=0,
        output_tokens=0,
        cost=0,
    )
    request = ModelRequest(
        messages=(),
        model="m",
        provider="p",
        request_id="request-1",
    )
    response = await kernel._consume(task, state, Provider(), request)
    assert any(isinstance(block, TextBlock) for block in response.blocks)
    assert any(isinstance(block, CapabilityCallBlock) for block in response.blocks)
