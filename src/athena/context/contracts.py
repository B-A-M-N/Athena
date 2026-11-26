"""Neutral context contracts shared by compiler and retrieval.

These types were previously compiler-local internals. Moving them here
removes the retrieval->compiler hidden backreference (review item 25).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from athena.protocol.capabilities import CapabilityDescriptor
from athena.protocol.messages import ContentBlock, Message, Provenance, Role, TrustClass
from athena.strategy import StrategyGuidance

__all__ = [
    "ContextEntry",
    "ContextStaticContext",
    "SkillRetrieval",
    "MemoryCacheKey",
    "MemoryRetrievalMode",
]


class MemoryRetrievalMode(str, enum.Enum):
    """Staged memory-retrieval gating (P1-12).

    EXPLICIT — the objective uses referential language ("remember",
    "the way we decided", "my usual format"): retrieve broadly; the
    referent plausibly lives in durable memory.
    WORK — ordinary repo/work turn: retrieve, but only content that
    strongly matches; the durable store is not scanned for every turn.
    SKIP — definitely a self-contained response turn (greeting, thanks):
    no retrieval.
    """

    EXPLICIT = "explicit"
    WORK = "work"
    SKIP = "skip"


@dataclass(frozen=True)
class ContextEntry:
    """Internal representation of one context element before final render."""

    name: str
    text: str
    tokens: int
    role: Role
    category: str
    trust: TrustClass
    mandatory: bool
    is_capability: bool = False
    provenance: Provenance | None = None
    created_at: Any = None
    value: float = 0.5
    message: Message | None = None  # original message kept verbatim
    blocks: tuple[ContentBlock, ...] | None = None
    droppable: bool = False  # memory/skill/artifact: removable under pressure
    cache_zone: str = "dynamic"  # stable prefix or dynamic/history suffix


@dataclass(frozen=True)
class MemoryCacheKey:
    """Cache key for memory retrieval results.

    Named (not a bare tuple) so future key-component changes surface as
    type errors instead of silently colliding tuple shapes.
    """

    task_id: str
    mode: str
    store_generation: int


@dataclass(frozen=True)
class SkillRetrieval:
    """Skills plus their explicit selection evidence."""

    skills: tuple[Any, ...] = ()
    records: tuple[Any, ...] = ()


@dataclass(frozen=True)
class ContextStaticContext:
    """Revisioned context material that is stable across model turns."""

    context_blocks: tuple[ContextEntry, ...] = ()
    memories: tuple[Any, ...] = ()
    skills: tuple[Any, ...] = ()
    skill_selection_records: tuple[Any, ...] = ()
    strategy_selection_record: Any | None = None
    research: tuple[ContextEntry, ...] = ()
    workflows: tuple[ContextEntry, ...] = ()
    capabilities: tuple[CapabilityDescriptor, ...] = ()
    discovery_state: str = "not_required"
    strategy: StrategyGuidance = field(
        default_factory=lambda: StrategyGuidance(
            route="respond", rationale="No external action or evidence acquisition is required."
        )
    )
