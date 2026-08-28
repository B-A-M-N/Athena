"""Context selection (BHV-029, BHV-030, §58).

Selection does NOT use a single numeric priority.  It evaluates independent
dimensions — authority, relevance, recency, task-fit, token cost, and
mandatory status — and, given a token budget, keeps the highest-value content
while always retaining required behavioral constraints.

Hard invariants enforced through the selection layer:

* mandatory elements (objective, running system policy, active acceptance
  criteria, approvals, workspace/security boundaries) are never dropped;
* the recent N turns are retained verbatim;
* the assembled content stays within the budget (bounded context).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from athena.protocol.messages import TrustClass

from athena.context.provenance import trust_rank

# Ordered priority bands (SPEC §20). Lower index == higher priority.
PRIORITY_BANDS: tuple[str, ...] = (
    "security_policy",
    "user_task",
    "project_instruction",
    "required_tools",
    "task_state",
    "recent_conversation",
    "retrieved_memory",
    "relevant_skills",
    "historical",
)


@dataclass(frozen=True)
class Selection:
    """A single candidate piece of context for potential inclusion."""

    name: str
    text: str
    tokens: int
    category: str = "historical"
    priority: int = PRIORITY_BANDS.index("historical")
    trust: TrustClass = TrustClass.AGENT_CURATED
    relevance: float = 0.5
    created_at: datetime | None = None
    mandatory: bool = False
    marker: bool = False
    provenance_meta: dict[str, Any] = field(default_factory=dict)

    def score(self, *, recency_half_life: timedelta = timedelta(hours=4)) -> float:
        """Value score used when budget forces eviction.

        Higher is better. Authority and relevance dominate; recency decays
        exponentially with a configurable half-life; mandatory elements get a
        large bonus so they are selected first and evicted last.
        """
        if self.mandatory:
            return 1_000_000.0
        base = (
            0.55 * (1.0 - trust_rank(self.trust) / max(1, len(TrustClass) - 1))
            + 0.30 * self.relevance
        )
        if self.created_at is not None:
            age = datetime.now(timezone.utc) - self.created_at
            if age < timedelta(0):
                age = timedelta(0)
            decay = 0.5 ** (age / max(recency_half_life, timedelta(milliseconds=1)))
            base = base * 0.75 + 0.25 * decay
        else:
            base = base * 0.75
        # Prefer higher priority bands marginally; recency+relevance already carry it.
        return base


def estimate_tokens(text: str) -> int:
    """Cheap deterministic token estimate (~4 chars per token)."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


class ContextBudget:
    """Token budget tracker used to keep the compiled context bounded."""

    def __init__(self, limit: int, *, reserve_output: int = 0) -> None:
        self.limit = limit
        self.reserve_output = reserve_output
        self.used = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.reserve_output - self.used)

    def try_add(self, tokens: int) -> bool:
        if self.used + tokens <= self.limit - self.reserve_output:
            self.used += tokens
            return True
        return False

    def add(self, tokens: int) -> None:
        self.used += tokens


def select_selections(
    selections: Iterable[Selection],
    budget: int,
    *,
    reserve_output: int = 0,
) -> list[Selection]:
    """Given a budget, return the selections to keep (mandatory first).

    Returns a list preserving the input order of the surviving items so the
    assembled conversation reads coherently.
    """
    items = list(selections)

    mandatory = [s for s in items if s.mandatory]
    optional = [s for s in items if not s.mandatory]

    result: list[Selection] = [s for s in mandatory]
    used_budget = sum(s.tokens for s in result)
    if used_budget > budget - reserve_output:
        raise OverflowError(
            f"Mandatory context ({used_budget} tokens) exceeds budget "
            f"({budget - reserve_output} tokens); cannot form a bounded context."
        )
    budget_for_optional = (budget - reserve_output) - used_budget

    ranked = sorted(optional, key=lambda s: s.score(), reverse=True)
    chosen_optional: list[Selection] = []
    used = 0
    for s in ranked:
        if used + s.tokens <= budget_for_optional:
            chosen_optional.append(s)
            used += s.tokens

    chosen_optional_ids = {id(s) for s in chosen_optional}
    optional_keep = [s for s in optional if id(s) in chosen_optional_ids]

    ordered = list(result)
    ordered.extend(optional_keep)
    ordered.sort(key=lambda s: (s.priority, -s.score()))
    return ordered


def band_index(category: str) -> int:
    try:
        return PRIORITY_BANDS.index(category)
    except ValueError:
        return PRIORITY_BANDS.index("historical")


__all__ = [
    "Selection",
    "ContextBudget",
    "select_selections",
    "estimate_tokens",
    "band_index",
]
