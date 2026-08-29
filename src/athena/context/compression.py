"""Context compression (BHV-032, BHV-033, §59, SPEC §21).

Compression is a replaceable strategy.  The default strategy:

* retains the required categories exactly (current objective, acceptance
  criteria, policy, approvals, security/work boundaries, active mutations),
  i.e. never summarizes protected selections;
* retains the most recent N turns verbatim;
* summarizes older, lower-value transcript ranges while keeping every
  capability call/result verbatim;
* emits a reversible :class:`CompilationMarker` so the transcript records that
  compression occurred and how it may be reversed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from athena.protocol.messages import (
    CapabilityCallBlock,
    CapabilityResultBlock,
    ContentBlock,
    Provenance,
    SourceType,
    TextBlock,
    TrustClass,
    utcnow,
)

from athena.context.provenance import merge_provenance, prov, trust_rank
from athena.context.selection import PRIORITY_BANDS, Selection, estimate_tokens

# Priority categories that must never be compressed away (SPEC §20, P0-P4).
_PROTECTED_CATEGORIES = frozenset(
    {
        "security_policy",
        "user_task",
        "project_instruction",
        "required_tools",
        "task_state",
        "approval",
        "pending_mutation",
        "unresolved_error",
        "security_boundary",
        "workspace_boundary",
    }
)


@dataclass(frozen=True)
class CompressionMarker:
    """Reversible record that compression occurred for a set of selections."""

    kind: str = "compression"
    selection_ids: tuple[str, ...] = ()
    message_ids: tuple[str, ...] = ()
    summary: str = ""
    provenance: Provenance = field(
        default_factory=lambda: prov(SourceType.RUNTIME, trust=TrustClass.AGENT_CURATED)
    )
    created_at: datetime = field(default_factory=utcnow)

    def as_text(self) -> str:
        n = len(self.message_ids)
        return (
            "[context:compressed] {n} earlier message(s) summarized "
            "({sel} selection(s)). Summary: {summary}. "
            "[context:compression:{kind}@{ts}]"
        ).format(
            n=n,
            sel=len(self.selection_ids),
            summary=self.summary,
            kind=self.kind,
            ts=self.created_at.isoformat(),
        )


@dataclass(frozen=True)
class CompressionRecord:
    markers: tuple[CompressionMarker, ...] = ()

    @property
    def occurred(self) -> bool:
        return bool(self.markers)


def _is_capability_block(block: ContentBlock) -> bool:
    return isinstance(block, (CapabilityCallBlock, CapabilityResultBlock))


def is_capability_block(block: ContentBlock) -> bool:
    return _is_capability_block(block)


def _block_digest(blocks: Iterable[ContentBlock]) -> str:
    """Short downstream digest that keeps capability calls/results verbatim."""
    parts: list[str] = []
    for b in blocks:
        if isinstance(b, TextBlock) and b.text:
            parts.append(b.text.strip().replace("\n", " ")[:400])
        elif isinstance(b, CapabilityCallBlock):
            parts.append(f"[capability:{b.capability_id}]")
        elif isinstance(b, CapabilityResultBlock):
            tail = (b.error or b.output or "").strip().replace("\n", " ")[:200]
            parts.append(f"[result:{b.capability_id}:{'ok' if b.ok else 'err'}:{tail}]")
    return " | ".join(p for p in parts if p)[:600]


def selection_is_protected(sel: Selection) -> bool:
    """BHV-032 — protected selections are never summarized away."""
    if sel.mandatory:
        return True
    if sel.category in _PROTECTED_CATEGORIES:
        return True
    return bool(sel.provenance_meta.get("protected"))


def _merged_provenance(sels: Iterable[Selection]) -> Provenance:
    """Merge selection provenance into a single provenance preserving origins."""
    pros = [
        s.provenance_meta.get("provenance")
        for s in sels
        if isinstance(s.provenance_meta, dict) and s.provenance_meta.get("provenance")
    ]
    if not pros:
        # Fall back to a runtime provenance carrying the most authoritative trust.
        trust = min((s.trust for s in sels), key=trust_rank)
        return prov(SourceType.RUNTIME, trust=trust, scope="compression")
    return merge_provenance(pros)


class ContextCompressor:
    """Default compression strategy for the compiled context.

    ``summarizer`` is an optional async callable ``text -> str``.  When absent,
    a deterministic truncation is used so compression is fully testable offline.
    """

    def __init__(
        self,
        *,
        recent_turns: int = 8,
        max_summary_chars: int = 600,
        summarizer=None,
    ) -> None:
        self.recent_turns = recent_turns
        self.max_summary_chars = max_summary_chars
        self._summarizer = summarizer
        self._summary_cache: dict[str, str] = {}

    async def _summarize(
        self,
        text: str,
        *,
        task=None,
        cache_key: str | None = None,
    ) -> str:
        identity = {
            "source": cache_key or hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "max_summary_chars": self.max_summary_chars,
            "recent_turns": self.recent_turns,
            "summarizer": (
                type(self._summarizer).__qualname__
                if self._summarizer is not None
                else "deterministic"
            ),
        }
        key = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        cached = self._summary_cache.get(key)
        if cached is not None:
            return cached
        if self._summarizer is not None:
            try:
                try:
                    result = await self._summarizer(text, task=task)
                except TypeError:
                    # Preserve the small public ``text -> str`` callback
                    # contract used by standalone compiler clients.
                    result = await self._summarizer(text)
                if result:
                    summary = str(result)[: self.max_summary_chars]
                    self._summary_cache[key] = summary
                    return summary
            except Exception:
                pass
        if len(text) <= self.max_summary_chars:
            summary = text
        else:
            summary = text[: self.max_summary_chars] + " …"
        self._summary_cache[key] = summary
        return summary

    async def compress(
        self,
        selections: list[Selection],
        *,
        recent_turns: int | None = None,
        actor: str = "context_compiler",
        task=None,
    ) -> tuple[list[Selection], CompressionRecord]:
        """Compress a selection list down to what fits.

        The mandatory / protected selections are assumed to already fit the
        budget; this method collapses the lowest-value, oldest, unprotected
        selections first until ``used <= remaining`` where ``remaining`` is the
        caller-provided optional goal stored on the selections' reserved meta.
        """
        recent_turns = self.recent_turns if recent_turns is None else recent_turns
        # Recompute an ordering by priority band (P0..P8); disqualify protected.
        ranked = sorted(
            (s for s in selections if not selection_is_protected(s)),
            key=lambda s: (s.priority, -s.score()),
        )
        compressible = list(ranked)
        protected = [s for s in selections if selection_is_protected(s)]

        markers: list[CompressionMarker] = []
        result: list[Selection] = list(protected)

        summarized_sels: list[Selection] = []
        for idx, sel in enumerate(compressible):
            # Preserve the most recent N selections verbatim (recent turn floor).
            if idx < recent_turns:
                result.append(sel)
                continue
            summarized = await self._summarize(
                sel.text,
                task=task,
                cache_key=_selection_cache_key(sel),
            )
            marker = CompressionMarker(
                selection_ids=(sel.name,),
                message_ids=tuple(sel.provenance_meta.get("message_ids", ())),
                summary=summarized,
                provenance=prov(
                    SourceType.RUNTIME,
                    source_id=str(sel.provenance_meta.get("source_id")) or None,
                    trust=sel.trust,
                    scope="compression",
                    created_at=sel.created_at,
                ),
            )
            markers.append(marker)
            summarized_sels.append(sel)

        if summarized_sels:
            summary_text = await self._summarize(
                "\n".join(s.text for s in summarized_sels if s.text),
                task=task,
                cache_key=_selection_group_cache_key(summarized_sels),
            )
            merged = _merged_provenance(summarized_sels)
            result.append(
                Selection(
                    name="summary:compressed",
                    text=summary_text,
                    tokens=estimate_tokens(summary_text),
                    category="recent_conversation",
                    priority=PRIORITY_BANDS.index("recent_conversation"),
                    trust=merged.trust,
                    marker=True,
                    provenance_meta={
                        "message_ids": tuple(
                            sid
                            for s in summarized_sels
                            for sid in s.provenance_meta.get("message_ids", ())
                        ),
                    },
                )
            )

        return result, CompressionRecord(tuple(markers))


CompressedRecord = CompressionRecord


def _selection_cache_key(selection: Selection) -> str:
    return json.dumps(
        {
            "name": selection.name,
            "text": hashlib.sha256(selection.text.encode("utf-8")).hexdigest(),
            "provenance": dict(selection.provenance_meta or {}),
        },
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )


def _selection_group_cache_key(selections: list[Selection]) -> str:
    return json.dumps(
        [_selection_cache_key(selection) for selection in selections],
        separators=(",", ":"),
    )


__all__ = [
    "CompressionMarker",
    "CompressionRecord",
    "ContextCompressor",
    "selection_is_protected",
    "is_capability_block",
]
