"""Durable attached context blocks.

Context blocks are intentionally narrower than memories: they are explicitly
attached operating context, not retrieval candidates.  The store owns the
version history; this module owns the value object used by the compiler and
the capability boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from athena.protocol.messages import Provenance, TrustClass, utcnow


@dataclass(frozen=True)
class ContextBlock:
    """One version of an explicitly attached context block."""

    id: str
    label: str
    content: str
    scope: str
    scope_id: str
    trust: TrustClass = TrustClass.AGENT_CURATED
    max_tokens: int = 2_500
    attached: bool = True
    version: int = 1
    provenance: Provenance | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime | None = None

    @property
    def effective_provenance(self) -> Provenance:
        if self.provenance is not None:
            return self.provenance
        from athena.context.provenance import prov
        from athena.protocol.messages import SourceType

        return prov(
            SourceType.TASK if self.scope == "task" else SourceType.MEMORY,
            source_id=self.id,
            trust=self.trust,
            scope=self.scope,
            created_at=self.created_at,
        )

    def bounded_content(self) -> str:
        """Return content bounded by the block's declared token budget."""
        return self.content[: max(1, self.max_tokens) * 4]

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "content": self.content,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "trust": self.trust.value,
            "max_tokens": self.max_tokens,
            "attached": self.attached,
            "version": self.version,
            "provenance": _provenance_record(self.effective_provenance),
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


def _provenance_record(value: Provenance | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "source_type": value.source_type.value,
        "source_id": value.source_id,
        "trust": value.trust.value,
        "scope": value.scope,
        "created_at": value.created_at.isoformat() if value.created_at else None,
    }


__all__ = ["ContextBlock"]
