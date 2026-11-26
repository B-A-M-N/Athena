"""Bounded context admission, compression, and exact final accounting."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from athena.context.compression import CompressionMarker, CompressionRecord
from athena.context.contracts import ContextEntry
from athena.context.provenance import merge_provenance, prov
from athena.context.selection import estimate_tokens
from athena.protocol.messages import Role, SourceType, TrustClass
from athena.protocol.tasks import TaskSpec

_PROTECTED_CATEGORIES = frozenset(
    {
        "approval",
        "pending_mutation",
        "unresolved_error",
        "security_boundary",
        "workspace_boundary",
    }
)


async def bound_and_compress(
    required: list[ContextEntry],
    corpus: list[ContextEntry],
    budget: int,
    *,
    task: TaskSpec | None,
    recent_verbatim_turns: int,
    compressor: Any,
) -> tuple[list[ContextEntry], CompressionRecord, list[str]]:
    """Keep mandatory/protected context and admit optional context incrementally."""
    budget = max(budget, 0)
    kept: list[ContextEntry] = []
    used = 0
    used_characters = 0
    rendered_cache: dict[int, str] = {}
    rendered_lengths: dict[int, int] = {}
    omitted: list[str] = []

    def try_append(entry: ContextEntry, *, conservative: bool = False) -> bool:
        nonlocal used, used_characters
        cache_key = id(entry)
        rendered = rendered_cache.get(cache_key)
        if rendered is None:
            rendered = _entry_text(entry)
            rendered_cache[cache_key] = rendered
            rendered_lengths[cache_key] = len(rendered)
        candidate_characters = used_characters + rendered_lengths[cache_key]
        if kept:
            candidate_characters += 2
        candidate_tokens = max(0, (candidate_characters + 3) // 4)
        if conservative:
            candidate_tokens += 1
        if candidate_tokens > budget:
            return False
        kept.append(entry)
        used_characters = candidate_characters
        used = candidate_tokens
        return True

    for entry in required:
        if not try_append(entry):
            raise OverflowError(
                "Required context categories exceed the model context window; "
                "cannot form a bounded context."
            )

    transcript = [entry for entry in corpus if not entry.droppable]
    droppable = [entry for entry in corpus if entry.droppable]
    cap_positions = [index for index, entry in enumerate(transcript) if entry.is_capability]
    cap_window = recent_verbatim_turns // 2
    cap_protected = {
        index
        for position in cap_positions
        for index in range(position - cap_window, position + cap_window + 1)
        if 0 <= index < len(transcript)
    }
    fence = len(transcript) - recent_verbatim_turns
    protected: list[ContextEntry] = []
    older: list[ContextEntry] = []
    for index, entry in enumerate(transcript):
        verbatim = (
            index >= fence or index in cap_protected or entry.category in _PROTECTED_CATEGORIES
        )
        (protected if verbatim else older).append(entry)

    for entry in protected:
        if not try_append(entry):
            raise OverflowError(
                "protected recent/capability context exceeds budget; "
                "cannot satisfy BHV-032 without overflow."
            )

    for entry in sorted(
        older,
        key=lambda item: (float(item.value), str(item.created_at or ""), item.name),
        reverse=True,
    ):
        if not try_append(entry, conservative=True):
            continue

    summarized_subject = [entry for entry in older if entry not in kept]
    markers: list[CompressionMarker] = []
    if summarized_subject:
        merged = _merged_provenance(summarized_subject)
        summary_budget = max(0, budget - used - (1 if kept else 0))
        summary_text = await compressor._summarize(
            "\n".join(entry.text for entry in summarized_subject if entry.text),
            task=task,
            cache_key=_entry_group_cache_key(summarized_subject, task=task),
            max_tokens=summary_budget,
        )
        if not summary_text:
            summary_text = (
                f"{len(summarized_subject)} older context entries omitted; "
                "recover from transcript anchors"
            )
        markers.append(
            CompressionMarker(
                message_ids=tuple(entry.name for entry in summarized_subject),
                summary=summary_text,
                provenance=merged,
            )
        )
        summary_entry = ContextEntry(
            name="summary:compressed",
            text=summary_text,
            tokens=estimate_tokens(summary_text),
            role=Role.COMPRESSION,
            category="recent_conversation",
            trust=merged.trust,
            mandatory=False,
            provenance=merged,
        )
        if summary_text and not try_append(summary_entry, conservative=True):
            omitted.append(summary_entry.name)

    for entry in sorted(droppable, key=lambda item: -item.value):
        if not try_append(entry, conservative=True):
            omitted.append(entry.name)

    used = estimate_tokens("\n\n".join(_entry_text(item) for item in kept))
    if used > budget:
        raise OverflowError(
            f"Compiled context exceeds input budget ({used} > {budget}); "
            "cannot form a bounded context."
        )
    return kept, CompressionRecord(tuple(markers), compressor.consume_degraded()), omitted


def _entry_text(entry: ContextEntry) -> str:
    return entry.message.conversation_text() if entry.message is not None else entry.text


def _merged_provenance(entries: Sequence[ContextEntry]):
    provenances = [entry.provenance for entry in entries if entry.provenance]
    if not provenances:
        return prov(SourceType.RUNTIME, trust=TrustClass.AGENT_CURATED, scope="compression")
    return merge_provenance(provenances)


def _entry_group_cache_key(entries: Sequence[ContextEntry], *, task: TaskSpec | None = None) -> str:
    metadata = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
    identity = {
        "entries": [
            {
                "id": entry.name,
                "content": hashlib.sha256(entry.text.encode("utf-8")).hexdigest(),
                "category": entry.category,
                "trust": str(entry.trust),
            }
            for entry in entries
        ],
        "compression_policy": {"compiler": "context-v1"},
        "summarizer_profile": metadata.get("model_profile")
        or metadata.get("summarizer_profile")
        or metadata.get("model_id"),
    }
    return json.dumps(identity, sort_keys=True, default=str, separators=(",", ":"))


__all__ = ["bound_and_compress"]
