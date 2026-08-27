from athena.models.registry import _collect_response
import pytest
from athena.protocol.messages import CapabilityCallBlock, TextBlock
from athena.protocol.models import (
    ModelDelta,
    ModelEvent,
    ModelEventType,
    ModelRequest,
    ModelResponse,
    ModelResponseAccumulator,
)


def _request() -> ModelRequest:
    return ModelRequest(
        messages=(), model="model", provider="provider", request_id="request",
    )


@pytest.mark.athena_scenario("COMPAT-001")
def test_accumulator_merges_mixed_stream_without_duplicate_text():
    request = _request()
    accumulator = ModelResponseAccumulator(request)
    call = CapabilityCallBlock(call_id="call-1", capability_id="files.read")
    accumulator.ingest(ModelEvent(
        type=ModelEventType.DELTA,
        request_id=request.request_id,
        delta=ModelDelta(request_id=request.request_id, text="hello"),
    ))
    accumulator.ingest(ModelEvent(
        type=ModelEventType.DELTA,
        request_id=request.request_id,
        delta=ModelDelta(request_id=request.request_id, block=call),
    ))
    accumulator.ingest(ModelEvent(
        type=ModelEventType.DONE,
        request_id=request.request_id,
        response=ModelResponse(
            request_id=request.request_id,
            model=request.model,
            provider=request.provider,
            blocks=(TextBlock(text="hello"),),
        ),
    ))

    response = accumulator.finish()
    assert [type(block) for block in response.blocks] == [TextBlock, CapabilityCallBlock]
    assert response.blocks[0].text == "hello"
    assert response.blocks[1].call_id == "call-1"


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
