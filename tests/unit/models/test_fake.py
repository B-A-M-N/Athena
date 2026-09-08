from athena.models.fake import FakeModelProvider
from athena.protocol.messages import CapabilityResultBlock, Message, Role, TextBlock
from athena.protocol.models import ModelEventType, ModelRequest


def _request(text: str = "hi") -> ModelRequest:
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
        model="fake",
        provider="fake",
        request_id="req-1",
    )


def _result_request(ok: bool = True) -> ModelRequest:
    return ModelRequest(
        messages=(
            Message(
                id="m2",
                role=Role.CAPABILITY,
                blocks=(CapabilityResultBlock(type="capability_result", ok=ok, output="data"),),
                created_at=None,
                provenance=None,
            ),
        ),
        model="fake",
        provider="fake",
        request_id="req-2",
    )


async def test_fake_returns_canned_response():
    provider = FakeModelProvider(
        scripts=[{"match": {"user_contains": "ping"}, "respond": {"text": "pong", "done": True}}],
        model="fake-1",
        provider="fake",
    )

    events = [e async for e in provider.complete(_request("ping"))]

    assert any(e.type == ModelEventType.DONE for e in events)
    done = next(e for e in events if e.type == ModelEventType.DONE)
    assert done.response is not None
    assert any(isinstance(b, TextBlock) and b.text == "pong" for b in done.response.blocks)
    assert done.response.usage is not None


async def test_scripted_mode_two_turns():
    """Turn 1: capability call; turn 2 (with result): final answer."""
    provider = FakeModelProvider(
        scripts=[
            {
                "match": {"user_contains": "start"},
                "respond": {
                    "capability_call": {"capability_id": "files.read", "arguments": {"p": "x"}},
                },
            },
            {
                "match": {"capability_result_ok": True},
                "respond": {"text": "final answer here", "done": True},
            },
        ],
        model="fake-1",
        provider="fake",
    )

    first = [e async for e in provider.complete(_request("start"))]
    call_blocks = [e.delta.block for e in first if e.delta and e.delta.block]
    assert any(getattr(b, "capability_id", None) == "files.read" for b in call_blocks)

    second = [e async for e in provider.complete(_result_request(True))]
    done = next(e for e in second if e.type == ModelEventType.DONE)
    assert any(
        isinstance(b, TextBlock) and b.text == "final answer here" for b in done.response.blocks
    )


async def test_fake_source_aware_history_predicates_use_last_matching_source():
    request = ModelRequest(
        messages=(
            Message(
                id="u1",
                role=Role.USER,
                blocks=(TextBlock(type="text", text="old user marker"),),
                created_at=None,
                provenance=None,
            ),
            Message(
                id="a1",
                role=Role.ASSISTANT,
                blocks=(TextBlock(type="text", text="old assistant marker"),),
                created_at=None,
                provenance=None,
            ),
            Message(
                id="r1",
                role=Role.CAPABILITY,
                blocks=(
                    CapabilityResultBlock(
                        type="capability_result", ok=False, output="old capability marker"
                    ),
                ),
                created_at=None,
                provenance=None,
            ),
            Message(
                id="u2",
                role=Role.USER,
                blocks=(TextBlock(type="text", text="last user marker"),),
                created_at=None,
                provenance=None,
            ),
            Message(
                id="a2",
                role=Role.ASSISTANT,
                blocks=(TextBlock(type="text", text="last assistant marker"),),
                created_at=None,
                provenance=None,
            ),
            Message(
                id="r2",
                role=Role.CAPABILITY,
                blocks=(
                    CapabilityResultBlock(
                        type="capability_result", ok=True, output="last capability marker"
                    ),
                ),
                created_at=None,
                provenance=None,
            ),
        ),
        model="fake",
        provider="fake",
        request_id="req-history",
    )
    provider = FakeModelProvider(
        scripts=[
            {
                "match": {
                    "last_user_message_contains": "last user",
                    "last_capability_result_contains": "last capability",
                    "assistant_message_contains": "last assistant",
                },
                "respond": {"text": "source-aware", "done": True},
            }
        ]
    )

    events = [event async for event in provider.complete(request)]
    done = next(event for event in events if event.type == ModelEventType.DONE)
    assert done.response is not None
    assert done.response.blocks[0].text == "source-aware"
