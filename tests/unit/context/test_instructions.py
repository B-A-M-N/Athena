from athena.context.instructions import (
    INSTRUCTION_ORDER,
    InstructionBlock,
    InstructionSet,
    provider_role_for_source,
)
from athena.protocol.messages import Role, TrustClass


def test_instruction_renderer_canonicalizes_conflict_order():
    sources = (
        "runtime_safety_policy",
        "explicit_user_instruction",
        "project_instruction",
        "activated_skill",
        "retrieved_context",
        "untrusted_text",
    )
    rendered = InstructionSet(
        tuple(InstructionBlock(text=f"rule-{source}", source=source) for source in reversed(sources))
    ).render()

    positions = [rendered.index(f"rule-{source}") for source in sources]
    assert positions == sorted(positions)
    assert "runtime safety policy" in rendered
    assert "Low-authority content never overrides" in rendered
    assert INSTRUCTION_ORDER.index("explicit_user_instruction") < INSTRUCTION_ORDER.index(
        "project_instruction"
    )


def test_provider_roles_are_decided_by_source_and_ownership():
    assert (
        provider_role_for_source(
            "runtime_safety_policy", scope="runtime", trust=TrustClass.AUTHORITY
        )
        is Role.SYSTEM
    )
    assert (
        provider_role_for_source(
            "runtime_guidance", scope="strategy", trust=TrustClass.CONFIGURED_INSTRUCTION
        )
        is Role.SYSTEM
    )
    for source, trust in (
        ("explicit_user_instruction", TrustClass.USER_CONTENT),
        ("project_instruction", TrustClass.CONFIGURED_INSTRUCTION),
        ("activated_skill", TrustClass.AGENT_CURATED),
        ("retrieved_context", TrustClass.EXTERNAL_CONTENT),
        ("untrusted_text", TrustClass.UNTRUSTED),
    ):
        assert provider_role_for_source(source, scope="task", trust=trust) is Role.USER
