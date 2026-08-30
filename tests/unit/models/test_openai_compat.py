import json

from athena.models.providers.openai_compat import (
    OpenAICompatProvider,
    parse_tool_arguments,
)
from athena.protocol.messages import (
    AudioBlock,
    CapabilityResultBlock,
    ImageBlock,
    Message,
    ReasoningBlock,
    Role,
    TextBlock,
)
from athena.protocol.models import ModelEventType, ModelRequest


class _FakeSSEResponse:
    """Fake httpx streaming response sur && aiter_lines().."""

    def __init__(self, lines, status_code=200, content_type="text/event-stream"):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _StreamCtx:
    """Patches ``client.stream`` (sync method → async ctx manager)."""

    def __init__(self, fake_resp):
        self._fake = fake_resp

    def __call__(self, *args, **kwargs):
        return _AsyncCtxManager(self._fake)


class _AsyncCtxManager:
    def __init__(self, fake):
        self._fake = fake

    async def __aenter__(self):
        return self._fake

    async def __aexit__(self, *exc):
        return False


def _provider():
    return OpenAICompatProvider(
        base_url="https://fake.invalid",
        api_key="test-key",
        model="gpt-test",
        provider="fake-openai",
    )


def _user_request(text: str = "hello") -> ModelRequest:
    return ModelRequest(
        messages=(
            Message(
                id="m1",
                role=Role.USER,
                blocks=(TextBlock(type="text", text=text),),
                created_at=None,
                provenance=None,
            ),
        ),
        model="gpt-test",
        provider="fake-openai",
        request_id="req-1",
        system="sys prompt",
    )


async def test_stream_events_delta_and_done_with_usage(monkeypatch):
    provider = _provider()
    sse = [
        "data: " + json.dumps({"choices": [{"delta": {"content": "Hello"}}]}),
        "data: "
        + json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 3, "cost_usd": 0.004}}),
        "data: [DONE]",
    ]
    fake_resp = _FakeSSEResponse(sse)
    monkeypatch.setattr(provider._client, "stream", _StreamCtx(fake_resp))

    events = [e async for e in provider.complete(_user_request())]

    text_deltas = [
        e for e in events if e.type == ModelEventType.DELTA and (e.delta.text or "") == "Hello"
    ]
    done = next(e for e in events if e.type == ModelEventType.DONE)
    assert len(text_deltas) == 1
    assert done.response is not None
    assert done.response.usage is not None
    assert done.response.usage.input_tokens == 10
    assert done.response.usage.output_tokens == 3
    assert done.response.usage.cost_usd == 0.004


def test_nonstream_response_preserves_provider_reported_cost():
    provider = _provider()
    event = provider._parse_complete(
        _user_request(),
        {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            "cost": 0.0007,
        },
    )

    assert event.response.usage.cost_usd == 0.0007


async def test_provider_response_keeps_profile_and_cache_metadata(monkeypatch):
    provider = _provider()
    base = _user_request()
    request = ModelRequest(
        messages=base.messages,
        model=base.model,
        provider=base.provider,
        request_id=base.request_id,
        system=base.system,
        metadata={
            "provider_profile_id": "openai-local",
            "provider_profile_fingerprint": "fp-1",
            "protocol": "openai-compat",
            "cache_session_key": "session:openai-local",
        },
    )
    sse = [
        "data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]}),
        "data: "
        + json.dumps(
            {
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 3,
                    "prompt_tokens_details": {"cached_tokens": 7},
                },
            }
        ),
        "data: [DONE]",
    ]
    monkeypatch.setattr(provider._client, "stream", _StreamCtx(_FakeSSEResponse(sse)))
    events = [e async for e in provider.complete(request)]
    response = next(e.response for e in events if e.response is not None)
    assert response.metadata["provider_profile_id"] == "openai-local"
    assert response.metadata["provider_profile_fingerprint"] == "fp-1"
    assert response.metadata["cache_session_key"] == "session:openai-local"


def test_translate_assistant_message_with_capability_call():
    from athena.protocol.messages import CapabilityCallBlock

    provider = _provider()
    msg = Message(
        id="m-a",
        role=Role.ASSISTANT,
        blocks=(
            CapabilityCallBlock(
                type="capability_call",
                call_id="call-1",
                capability_id="files.read",
                arguments={"path": "/tmp/x"},
            ),
        ),
        created_at=None,
        provenance=None,
    )
    translated = provider._translate_message(msg)

    assert translated["role"] == "assistant"
    assert "tool_calls" in translated
    assert translated["tool_calls"][0]["function"]["name"] == "files.read"
    assert json.loads(translated["tool_calls"][0]["function"]["arguments"]) == {"path": "/tmp/x"}


def test_translate_capability_result_to_tool_message():
    provider = _provider()
    msg = Message(
        id="m-r",
        role=Role.CAPABILITY,
        blocks=(
            CapabilityResultBlock(
                type="capability_result",
                call_id="call-1",
                capability_id="files.read",
                ok=True,
                output="file contents",
            ),
        ),
        created_at=None,
        provenance=None,
    )
    translated = provider._translate_message(msg)

    assert isinstance(translated, list)
    assert translated[0]["role"] == "tool"
    assert translated[0]["tool_call_id"] == "call-1"


def test_tool_argument_parse_failure_is_not_an_empty_object():
    assert parse_tool_arguments("{broken") is None
    assert parse_tool_arguments("{}") == {}


def test_nonstream_response_keeps_reasoning_content():
    provider = _provider()
    event = provider._parse_complete(
        _user_request(),
        {
            "choices": [
                {
                    "message": {
                        "reasoning_content": "inspect first",
                        "content": "I will inspect it.",
                    }
                }
            ]
        },
    )

    assert isinstance(event.response.blocks[0], ReasoningBlock)
    assert event.response.blocks[0].text == "inspect first"
    assert event.response.blocks[1].text == "I will inspect it."


def test_translate_multimodal_blocks_without_dropping_them():
    provider = _provider()
    message = Message(
        id="m-media",
        role=Role.USER,
        blocks=(
            TextBlock(text="inspect these"),
            ImageBlock(data_path="https://example.test/image.png", mime_type="image/png"),
            AudioBlock(data_path="BASE64", mime_type="audio/wav"),
        ),
        created_at=None,
        provenance=None,
    )

    translated = provider._translate_message(message)
    assert translated["content"] == [
        {"type": "text", "text": "inspect these"},
        {"type": "image_url", "image_url": {"url": "https://example.test/image.png"}},
        {"type": "input_audio", "input_audio": {"data": "BASE64", "format": "wav"}},
    ]
