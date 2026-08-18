"""Provenance model and utilities (BHV-033).

Every block injected into a compiled context MUST remain attributable to its
source even after summarization/compression.  Provenance preservation means:

* each injected block carries a :class:`Provenance`;
* when a block is summarized, the summary inherits (and merges) the original
  provenance;
* authority vs user content vs curated knowledge vs external vs untrusted is
  preserved so policy and context handling can act on the distinction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Mapping

from athena.protocol.messages import Provenance, SourceType, TrustClass, utcnow


# Ordered from most authoritative to least authoritative (BHV-031 / §57).
TRUST_ORDER: tuple[TrustClass, ...] = (
    TrustClass.AUTHORITY,
    TrustClass.USER_CONTENT,
    TrustClass.CONFIGURED_INSTRUCTION,
    TrustClass.AGENT_CURATED,
    TrustClass.EXTERNAL_CONTENT,
    TrustClass.UNTRUSTED,
)

_TRUST_RANK: dict[TrustClass, int] = {t: i for i, t in enumerate(TRUST_ORDER)}


def trust_rank(trust: TrustClass | None) -> int:
    """Higher rank == higher authority (0 is the most authoritative)."""
    if trust is None:
        return _TRUST_RANK[TrustClass.UNTRUSTED]
    return _TRUST_RANK.get(trust, _TRUST_RANK[TrustClass.UNTRUSTED])


def dominates(a: TrustClass | None, b: TrustClass | None) -> bool:
    """True if trust `a` has strictly higher authority than trust ``b``."""
    return trust_rank(a) < trust_rank(b)


def prov(
    source_type: SourceType,
    *,
    source_id: str | None = None,
    trust: TrustClass = TrustClass.AGENT_CURATED,
    scope: str | None = None,
    created_at: datetime | None = None,
) -> Provenance:
    """Convenience builder for a provenance record."""
    return Provenance(
        source_type=source_type,
        source_id=source_id,
        trust=trust,
        scope=scope,
        created_at=created_at or utcnow(),
    )


def merge_provenance(items: Iterable[Provenance | None]) -> Provenance:
    """Merge a set of provenances into one that preserves all origins.

    The merged provenance takes the *most authoritative* trust class and the
    earliest creation time, records every contributing source id, and marks
    the source type as the dominant one when it is uniform.
    """
    present = [p for p in items if p is not None]
    if not present:
        return prov(SourceType.RUNTIME, trust=TrustClass.AGENT_CURATED)

    merged_trust = min((p.trust for p in present), key=lambda t: _TRUST_RANK[t])
    merged_created = min((p.created_at for p in present if p.created_at is not None), default=None)

    primary = present[0]
    sources = [p.source_id for p in present if p.source_id]
    scope = primary.scope
    if len({p.scope for p in present if p.scope}) == 1:
        scope = primary.scope

    # If source ids differ, keep the full origin list joined so provenance is
    # never lost during compression.
    source_id: str | None
    if len(sources) == 1:
        source_id = sources[0]
    else:
        source_id = ",".join(s for s in sources if s)

    return prov(
        primary.source_type,
        source_id=source_id,
        trust=merged_trust,
        scope=scope,
        created_at=merged_created,
    )


def provenance_from_mapping(data: Mapping) -> Provenance:
    """Rebuild a Provenance from an arbitrary mapping (e.g. store JSON)."""
    source_type = SourceType(data.get("source_type", "runtime"))
    raw = data.get("trust", "agent_curated")
    try:
        trust = TrustClass(raw)
    except ValueError:
        trust = TrustClass.AGENT_CURATED
    created = data.get("created_at")
    if isinstance(created, str):
        created = datetime.fromisoformat(created)
    return Provenance(
        source_type=source_type,
        source_id=data.get("source_id"),
        trust=trust,
        scope=data.get("scope"),
        created_at=created,
    )


def block_trust(block_metadata: Mapping) -> TrustClass:
    raw = block_metadata.get("trust")
    if isinstance(raw, TrustClass):
        return raw
    if isinstance(raw, str):
        try:
            return TrustClass(raw)
        except ValueError:
            return TrustClass.AGENT_CURATED
    return TrustClass.AGENT_CURATED


@dataclass(frozen=True)
class ProvenanceMap:
    """Lookup for the provenance of every content block in a compiled context."""

    block_id: Mapping[str, Provenance] = field(default_factory=dict)

    def get(self, block_id: str) -> Provenance | None:
        return self.block_id.get(block_id)

    def __len__(self) -> int:
        return len(self.block_id)


__all__ = [
    "TRUST_ORDER",
    "trust_rank",
    "dominates",
    "prov",
    "merge_provenance",
    "provenance_from_mapping",
    "block_trust",
    "ProvenanceMap",
]