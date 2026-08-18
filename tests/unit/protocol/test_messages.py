from athena.protocol.messages import (
    CapabilityResultBlock,
    Message,
    Provenance,
    ReasoningBlock,
    Role,
    SourceType,
    TextBlock,
    utcnow,
)


def _prov():
    return Provenance(source_type=SourceType.RUNTIME)


def test_message_text_with_text_block():
    msg = Message(
        id="msg_1",
        role=Role.USER,
        blocks=(TextBlock(text="hello"), TextBlock(text="world")),
        created_at=utcnow(),
        provenance=_prov(),
    )
    assert msg.text() == "hello\nworld"


def test_message_text_with_capability_result_block_uses_output():
    msg = Message(
        id="msg_2",
        role=Role.CAPABILITY,
        blocks=(CapabilityResultBlock(output="cap output"),),
        created_at=utcnow(),
        provenance=_prov(),
    )
    assert msg.text() == "cap output"


def test_message_text_ignores_empty_and_uses_reasoning():
    msg = Message(
        id="msg_3",
        role=Role.ASSISTANT,
        blocks=(TextBlock(text=""), ReasoningBlock(text="think"), TextBlock(text="")),
        created_at=utcnow(),
        provenance=_prov(),
    )
    assert msg.text() == "think"