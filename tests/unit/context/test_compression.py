from datetime import datetime, timedelta, timezone

from athena.context.compression import ContextCompressor
from athena.context.selection import Selection
from athena.protocol.messages import TrustClass


def _sel(
    name: str,
    text: str,
    *,
    category: str = "recent_conversation",
    mandatory: bool = False,
    created_at=None,
    protected: bool = False,
) -> Selection:
    return Selection(
        name=name,
        text=text,
        tokens=max(1, len(text) // 4),
        category=category,
        trust=TrustClass.AGENT_CURATED,
        created_at=created_at,
        mandatory=mandatory,
        provenance_meta={"protected": protected, "message_ids": (name,)},
    )


async def test_compressing_older_turns_recap_present():
    """BHV-032/033: older turns are summarized, not lost — recap present."""
    now = datetime.now(timezone.utc)
    compressor = ContextCompressor(recent_turns=2)

    t1 = _sel("t1", "earlier one turn " + "x" * 200, created_at=now - timedelta(hours=5))
    t2 = _sel("t2", "earlier two turn " + "y" * 200, created_at=now - timedelta(hours=4))
    r1 = _sel("r1", "recency one", created_at=now - timedelta(minutes=5))
    r2 = _sel("r2", "recency two", created_at=now - timedelta(minutes=1))

    result, record = await compressor.compress([t1, t2, r1, r2])

    assert record.occurred
    joined = "\n".join(s.text for s in result)
    assert any(s.name == "summary:compressed" for s in result)
    assert "earlier one turn" in joined
    assert "recency two" in joined


async def test_protected_items_never_dropped():
    """BHV-032: mandatory and approved selections survive compression."""
    now = datetime.now(timezone.utc)
    compressor = ContextCompressor(recent_turns=0)

    objective = _sel(
        "objective", "the mission objective text",
        category="user_task", mandatory=True, created_at=now,
    )
    approval = _sel(
        "approval", "approval payload", category="approval", created_at=now,
    )
    old = _sel("old", "d " * 300, created_at=now - timedelta(hours=9))

    result, _record = await compressor.compress([old, objective, approval])

    names = [s.name for s in result]
    assert "objective" in names
    assert "approval" in names
    # The older droppable content is not silently retained verbatim.
    assert "old" not in names


async def test_small_transcript_has_recap():
    """Build a small transcript, compress, verify output has the recap."""
    now = datetime.now(timezone.utc)
    compressor = ContextCompressor(recent_turns=1, max_summary_chars=500)

    a = _sel("a", "first user message with facts", created_at=now - timedelta(hours=3))
    b = _sel("b", "second user message with details", created_at=now - timedelta(hours=2))
    c = _sel("c", "latest message now", created_at=now - timedelta(minutes=1))

    result, record = await compressor.compress([a, b, c])

    assert record.occurred
    joined = "\n".join(s.text for s in result)
    assert "latest message now" in joined
    assert "first user message with facts" in joined