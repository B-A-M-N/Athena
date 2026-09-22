from athena.models.registry import _collect_response
import pytest
from athena.kernel.inference_stream import consume_provider_stream
from athena.protocol.errors import ModelUnavailable, ProviderError, ProviderOutcomeUnknown
from athena.protocol.messages import CapabilityCallBlock, TextBlock
from athena.protocol.models import (
    IncompleteModelResponse,
    ModelDelta,
    ModelEvent,
    ModelEventType,
    ModelRequest,
    ModelResponse,
    ModelResponseAccumulator,
)


def _request() -> ModelRequest:
    return ModelRequest(
        messages=(),
        model="model",
        provider="provider",
        request_id="request",
    )


def _limited_request(limit: int) -> ModelRequest:
    return ModelRequest(
        messages=(),
        model="model",
        provider="provider",
        request_id="request",
        max_tokens=limit,
    )


@pytest.mark.athena_scenario("COMPAT-001")
def test_accumulator_merges_mixed_stream_without_duplicate_text():
    request = _request()
    accumulator = ModelResponseAccumulator(request)
    call = CapabilityCallBlock(call_id="call-1", capability_id="files.read")
    accumulator.ingest(
        ModelEvent(
            type=ModelEventType.DELTA,
            request_id=request.request_id,
            delta=ModelDelta(request_id=request.request_id, text="hello"),
        )
    )
    accumulator.ingest(
        ModelEvent(
            type=ModelEventType.DELTA,
            request_id=request.request_id,
            delta=ModelDelta(request_id=request.request_id, block=call),
        )
    )
    accumulator.ingest(
        ModelEvent(
            type=ModelEventType.DONE,
            request_id=request.request_id,
            response=ModelResponse(
                request_id=request.request_id,
                model=request.model,
                provider=request.provider,
                blocks=(TextBlock(text="hello"),),
            ),
        )
    )

    response = accumulator.finish()
    assert [type(block) for block in response.blocks] == [TextBlock, CapabilityCallBlock]
    assert response.blocks[0].text == "hello"
    assert response.blocks[1].call_id == "call-1"


def test_accumulator_rejects_assembly_without_terminal_provider_event():
    request = _request()
    accumulator = ModelResponseAccumulator(request)
    accumulator.ingest(
        ModelEvent(
            type=ModelEventType.DELTA,
            request_id=request.request_id,
            delta=ModelDelta(request_id=request.request_id, text="partial"),
        )
    )

    with pytest.raises(IncompleteModelResponse) as raised:
        accumulator.finish()

    assert raised.value.partial_response.finish_reason is None
    assert raised.value.diagnostic["text"] == "partial"


def test_accumulator_preserves_ordered_mixed_terminal_blocks():
    request = _request()
    accumulator = ModelResponseAccumulator(request)
    call = CapabilityCallBlock(call_id="call-ordered", capability_id="files.read")
    accumulator.ingest(
        ModelEvent(
            type=ModelEventType.DELTA,
            request_id=request.request_id,
            delta=ModelDelta(request_id=request.request_id, text="A"),
        )
    )
    accumulator.ingest(
        ModelEvent(
            type=ModelEventType.DELTA,
            request_id=request.request_id,
            delta=ModelDelta(request_id=request.request_id, block=call),
        )
    )
    accumulator.ingest(
        ModelEvent(
            type=ModelEventType.DELTA,
            request_id=request.request_id,
            delta=ModelDelta(request_id=request.request_id, text="B"),
        )
    )
    accumulator.ingest(
        ModelEvent(
            type=ModelEventType.DONE,
            request_id=request.request_id,
            response=ModelResponse(
                request_id=request.request_id,
                model=request.model,
                provider=request.provider,
                blocks=(TextBlock(text="A"), call, TextBlock(text="B")),
            ),
        )
    )

    response = accumulator.finish()
    assert [(type(block), getattr(block, "text", None)) for block in response.blocks] == [
        (TextBlock, "A"),
        (CapabilityCallBlock, None),
        (TextBlock, "B"),
    ]


def test_repeated_terminal_events_do_not_replace_the_first_response():
    request = _request()
    accumulator = ModelResponseAccumulator(request)
    for text in ("first", "second"):
        accumulator.ingest(
            ModelEvent(
                type=ModelEventType.DONE,
                request_id=request.request_id,
                response=ModelResponse(
                    request_id=request.request_id,
                    model=request.model,
                    provider=request.provider,
                    blocks=(TextBlock(text=text),),
                ),
            )
        )
    assert accumulator.finish().blocks == (TextBlock(text="first"),)


class _StreamKernel:
    _model_sink = None

    def _remaining_runtime_seconds(self, task, state):
        return None

    async def _emit(self, event_type, payload, task):
        return None


class _StreamBroker:
    def __init__(self):
        self._k = _StreamKernel()

    async def _relay_delta(self, task, delta):
        return None

    async def _fault_point(self, name):
        return None


class _StreamState:
    def __init__(self):
        import asyncio

        self.cancel = asyncio.Event()
        self.input_tokens = 0
        self.output_tokens = 0


class _PartialProvider:
    async def complete(self, request):
        yield ModelEvent(
            type=ModelEventType.DELTA,
            request_id=request.request_id,
            delta=ModelDelta(request_id=request.request_id, text="partial"),
        )


class _FailedProvider:
    async def complete(self, request):
        yield ModelEvent(
            type=ModelEventType.DELTA,
            request_id=request.request_id,
            delta=ModelDelta(request_id=request.request_id, text="partial"),
        )
        yield ModelEvent(
            type=ModelEventType.FAILED,
            request_id=request.request_id,
            error="provider rejected the request",
            code="provider_rejected",
        )


class _EmptyProvider:
    async def complete(self, request):
        if False:
            yield request


class _OversizedProvider:
    def __init__(self):
        self.cancelled = False

    async def complete(self, request):
        yield ModelEvent(
            type=ModelEventType.DELTA,
            request_id=request.request_id,
            delta=ModelDelta(request_id=request.request_id, text="ok"),
        )
        yield ModelEvent(
            type=ModelEventType.DELTA,
            request_id=request.request_id,
            delta=ModelDelta(request_id=request.request_id, text="x" * 100),
        )

    async def cancel(self, request_id):
        self.cancelled = True


@pytest.mark.asyncio
async def test_stream_without_terminal_event_is_unknown_when_partial_output_exists():
    with pytest.raises(ProviderOutcomeUnknown) as raised:
        await consume_provider_stream(
            _StreamBroker(),
            object(),
            _StreamState(),
            _PartialProvider(),
            _request(),
        )

    assert raised.value.data["partial_output"]["text"] == "partial"


@pytest.mark.asyncio
async def test_clean_empty_stream_is_a_provider_failure_not_unknown():
    with pytest.raises(ModelUnavailable):
        await consume_provider_stream(
            _StreamBroker(),
            object(),
            _StreamState(),
            _EmptyProvider(),
            _request(),
        )


@pytest.mark.asyncio
async def test_explicit_provider_failure_preserves_partial_output_as_diagnostic():
    with pytest.raises(ProviderError) as raised:
        await consume_provider_stream(
            _StreamBroker(),
            object(),
            _StreamState(),
            _FailedProvider(),
            _request(),
        )

    assert raised.value.data["partial_output"]["text"] == "partial"


@pytest.mark.asyncio
async def test_stream_output_limit_cancels_provider_and_preserves_partial_diagnostic():
    provider = _OversizedProvider()
    with pytest.raises(ProviderOutcomeUnknown) as raised:
        await consume_provider_stream(
            _StreamBroker(),
            object(),
            _StreamState(),
            provider,
            _limited_request(1),
        )

    assert provider.cancelled is True
    assert raised.value.data["partial_output"]["text"] == "ok"
    assert raised.value.data["output_token_limit"] == 1


@pytest.mark.asyncio
async def test_registry_invoke_uses_the_same_accumulator():
    class Provider:
        async def complete(self, request):
            yield ModelEvent(
                type=ModelEventType.DELTA,
                request_id=request.request_id,
                delta=ModelDelta(request_id=request.request_id, text="hello"),
            )
            yield ModelEvent(
                type=ModelEventType.DONE,
                request_id=request.request_id,
                response=ModelResponse(
                    request_id=request.request_id,
                    model=request.model,
                    provider=request.provider,
                    blocks=(),
                ),
            )

    response = await _collect_response(Provider(), _request())
    assert response.blocks[0].text == "hello"


@pytest.mark.asyncio
async def test_registry_invoke_cancels_and_reports_oversized_stream():
    provider = _OversizedProvider()
    with pytest.raises(ProviderOutcomeUnknown) as raised:
        await _collect_response(provider, _limited_request(1))

    assert provider.cancelled is True
    assert raised.value.data["partial_output"]["text"] == "ok"
