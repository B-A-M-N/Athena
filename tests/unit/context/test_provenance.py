
from athena.context.provenance import TRUST_ORDER, merge_provenance, prov, trust_rank
from athena.protocol.messages import SourceType, TrustClass, utcnow


def test_trust_order_user_content_above_configured_instruction():
    """BHV-031: user content outranks configured instructions."""
    u = TrustClass.USER_CONTENT
    c = TrustClass.CONFIGURED_INSTRUCTION
    assert TRUST_ORDER.index(u) < TRUST_ORDER.index(c)
    assert trust_rank(u) < trust_rank(c)


def test_authority_is_highest_trust():
    assert TRUST_ORDER[0] is TrustClass.AUTHORITY
    assert trust_rank(TrustClass.AUTHORITY) == 0
    for t in TRUST_ORDER[1:]:
        assert trust_rank(TrustClass.AUTHORITY) < trust_rank(t)


def test_merge_provenance_combines_multiple_without_crashing():
    p1 = prov(SourceType.TASK, source_id="task-1", trust=TrustClass.USER_CONTENT)
    p2 = prov(
        SourceType.MEMORY,
        source_id="mem-1",
        trust=TrustClass.AGENT_CURATED,
        created_at=utcnow(),
    )
    p3 = None

    merged = merge_provenance([p1, p2, p3])

    assert merged.trust is TrustClass.USER_CONTENT
    assert "task-1" in merged.source_id
    assert "mem-1" in merged.source_id


def test_merge_provenance_empty_falls_back_to_runtime():
    merged = merge_provenance([])
    assert merged.trust is TrustClass.AGENT_CURATED
    assert merged.source_type is SourceType.RUNTIME