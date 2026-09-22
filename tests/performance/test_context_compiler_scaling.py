"""Scaling and retention evidence for bounded context admission."""

from __future__ import annotations

import time

import pytest

from athena.context.compiler import ContextCompiler
from athena.context.contracts import ContextEntry
from athena.context.selection import estimate_tokens
from athena.protocol.messages import Role, SourceType, TrustClass, utcnow
from athena.context.provenance import prov


def _entry(index: int, *, category: str = "historical", capability: bool = False) -> ContextEntry:
    return ContextEntry(
        name=f"entry-{index}",
        text=f"entry {index} " + ("x" * 2990),
        tokens=751,
        role=Role.USER,
        category=category,
        trust=TrustClass.USER_CONTENT,
        mandatory=category in {"approval", "security_boundary"},
        is_capability=capability,
        provenance=prov(SourceType.SESSION, trust=TrustClass.USER_CONTENT),
        created_at=utcnow(),
    )


@pytest.mark.parametrize("count", (100, 400, 1600))
@pytest.mark.asyncio
async def test_context_admission_scales_linearly_enough(count: int) -> None:
    compiler = ContextCompiler(recent_verbatim_turns=0)
    entries = [_entry(index) for index in range(count)]
    started = time.perf_counter()
    kept, _record, _omitted = await compiler._bound_and_compress(
        [], entries, estimate_tokens("\n\n".join(entry.text for entry in entries)) + count
    )
    elapsed = time.perf_counter() - started
    assert len(kept) == count
    # This is a deliberately loose guard against quadratic rescanning.  It is
    # stable on slow CI while still rejecting the former ~N² path.
    if count == 1600:
        assert elapsed < 8.0


@pytest.mark.asyncio
async def test_incremental_admission_retains_same_hard_context_as_naive_ordering():
    compiler = ContextCompiler(recent_verbatim_turns=2)
    required = [_entry(0, category="security_boundary")]
    corpus = [
        _entry(1),
        _entry(2, category="approval"),
        _entry(3, capability=True),
        _entry(4),
        _entry(5),
        _entry(6),
        _entry(7),
        _entry(8),
    ]
    hard_items = required + corpus[1:4] + corpus[-2:]
    budget = estimate_tokens("\n\n".join(item.text for item in hard_items)) + 1

    kept, _record, _omitted = await compiler._bound_and_compress(
        required, corpus, budget
    )
    hard_names = {item.name for item in required}
    hard_names.update(item.name for item in corpus[1:4])
    hard_names.update(item.name for item in corpus[-2:])
    assert hard_names <= {item.name for item in kept}
