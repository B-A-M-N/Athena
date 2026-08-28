from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from athena.protocol.memory import MemoryKind, MemoryRecord, MemoryScope
from athena.protocol.messages import TrustClass

if TYPE_CHECKING:
    from athena.memory.store import MemoryStore

_WORD_RE = re.compile(r"[a-z0-9_']+")


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_WORD_RE.findall((text or "").lower()))


def _signature(record: MemoryRecord) -> frozenset[str]:
    tokens = set(_tokens(record.content))
    if record.summary:
        tokens.update(_tokens(record.summary))
    return frozenset(tokens)


def _scope_id(record: MemoryRecord) -> str | None:
    if "scope_id" in record.metadata:
        return str(record.metadata["scope_id"])
    if record.scope in (MemoryScope.TASK, MemoryScope.SESSION) and record.source:
        return record.source.source_id
    return None


class ConflictResolution(str, enum.Enum):
    NONE = "none"
    SUPERSEDE = "supersede"
    FLAG = "flag"
    REJECT = "reject"


# Higher rank == more trusted. A lower-trust memory never overwrites a
# higher-trust memory. AUTHORITY is the most trusted; UNTRUSTED the least.
_TRUST_RANK: dict[TrustClass, int] = {
    TrustClass.AUTHORITY: 5,
    TrustClass.CONFIGURED_INSTRUCTION: 4,
    TrustClass.USER_CONTENT: 3,
    TrustClass.AGENT_CURATED: 2,
    TrustClass.EXTERNAL_CONTENT: 1,
    TrustClass.UNTRUSTED: 0,
}


def _trust_rank(t: TrustClass | None) -> int:
    return _TRUST_RANK.get(t or TrustClass.AGENT_CURATED, 0)


@dataclass(frozen=True)
class ConflictReport:
    record: MemoryRecord
    conflicting: tuple[MemoryRecord, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class ConflictResult:
    resolution: ConflictResolution
    report: ConflictReport
    superseded: tuple[MemoryRecord, ...] = ()


class MemoryConflictResolver:
    """Resolves memory conflicts per truthfulness (BUILDSPEC 63, BHV-102).

    Equal-trust contradictions are FLAGGED (both preserved), never silently
    overwritten. A higher-trust record may SUPERSEDE a lower-trust one. A
    lower-trust record may never overwrite a higher-trust one and is REJECTed
    until explicitly promoted.
    """

    def __init__(self, store: "MemoryStore") -> None:
        self._store = store

    async def detect_conflict(self, record: MemoryRecord) -> ConflictReport:
        if record.kind is not MemoryKind.SEMANTIC:
            return ConflictReport(record=record)
        existing = await self._store.list_by_kind(MemoryKind.SEMANTIC)
        scope_id = _scope_id(record)
        new_key = _signature(record)
        conflicts: list[MemoryRecord] = []
        for old in existing:
            if old.id == record.id:
                continue
            if old.scope is not record.scope:
                continue
            if scope_id and _scope_id(old) != scope_id:
                continue
            old_key = _signature(old)
            overlap = len(new_key & old_key)
            if overlap < max(2, len(new_key) // 2) and overlap < max(2, len(old_key) // 2):
                continue
            conflicts.append(old)
        reason = "conflicting semantic memories in same scope" if conflicts else None
        return ConflictReport(record=record, conflicting=tuple(conflicts), reason=reason)

    async def resolve(self, record: MemoryRecord, report: ConflictReport) -> ConflictResult:
        if not report.conflicting:
            return ConflictResult(ConflictResolution.NONE, report)
        new_rank = _trust_rank(record.trust)
        higher = [c for c in report.conflicting if _trust_rank(c.trust) > new_rank]
        equal = [c for c in report.conflicting if _trust_rank(c.trust) == new_rank]
        lower = [c for c in report.conflicting if _trust_rank(c.trust) < new_rank]
        if higher:
            return ConflictResult(ConflictResolution.REJECT, report, superseded=tuple(higher))
        if equal:
            return ConflictResult(ConflictResolution.FLAG, report, superseded=tuple(equal))
        return ConflictResult(ConflictResolution.SUPERSEDE, report, superseded=tuple(lower))


__all__ = [
    "MemoryConflictResolver",
    "ConflictReport",
    "ConflictResult",
    "ConflictResolution",
]
