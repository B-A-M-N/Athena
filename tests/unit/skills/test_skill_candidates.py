from athena.protocol.messages import CapabilityCallBlock, CapabilityResultBlock
from athena.skills.candidates import candidates_from_task


async def test_no_capability_evidence_cannot_create_skill_candidate():
    candidates = await candidates_from_task(
        "say hello",
        ["I can answer this directly."],
        None,
    )

    assert candidates == []


async def test_two_successful_ordinary_calls_can_create_skill_candidate():
    transcript = [
        type(
            "MessageLike",
            (),
            {
                "blocks": (
                    CapabilityCallBlock(call_id="one", capability_id="fs", arguments={}),
                    CapabilityResultBlock(
                        call_id="one", capability_id="fs", ok=True, output="read"
                    ),
                )
            },
        )(),
        type(
            "MessageLike",
            (),
            {
                "blocks": (
                    CapabilityCallBlock(call_id="two", capability_id="execute", arguments={}),
                    CapabilityResultBlock(
                        call_id="two", capability_id="execute", ok=True, output="passed"
                    ),
                )
            },
        )(),
    ]

    candidates = await candidates_from_task(
        "follow these repeatable steps to inspect and verify the project",
        transcript,
        None,
    )

    assert len(candidates) == 1
