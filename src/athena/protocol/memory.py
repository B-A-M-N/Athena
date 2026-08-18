"""Canonical memory record model.

Memory is a first-class protocol type. Records are immutable and carry the
provenance and trust metadata needed to separate durable fact from speculation
(BUILDSPEC section 61-63).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from athena.protocol.messages import Provenance, TrustClass, utcnow


class MemoryKind(str, enum.Enum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"


class MemoryScope(str, enum.Enum):
    SESSION = "session"
    TASK = "task"
    PROJECT = "project"
    GLOBAL = "global"


class RetrievalMode(str, enum.Enum):
    EXACT = "exact"
    SEMANTIC = "semantic"
    RECENCY = "recency"


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    kind: MemoryKind
    scope: MemoryScope
    content: str
    summary: str | None = None
    source: Provenance | None = None
    trust: TrustClass = TrustClass.AGENT_CURATED
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    retrieval_mode: RetrievalMode | None = None
    subject: str | None = None
    tags: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    confidence: float | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    supersedes: tuple[str, ...] = ()
    contradicted_by: tuple[str, ...] = ()


__all__ = [
    "MemoryKind",
    "MemoryScope",
    "RetrievalMode",
    "MemoryRecord",
]