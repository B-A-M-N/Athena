from __future__ import annotations

import re
from typing import Any, Iterable

from athena.protocol.memory import MemoryKind, MemoryRecord, MemoryScope
from athena.protocol.messages import Provenance, SourceType, TrustClass, utcnow
from athena.protocol.tasks import TaskResult, TaskSpec
from athena.memory.store import new_memory_id

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_SUBJECTIVE = re.compile(
    r"\b(maybe|perhaps|probably|i think|i guess|i believe|could|might|"
    r"possibly|hopefully|seems|lots|sort of|kind of|not sure|uncertain)\b",
    re.IGNORECASE,
)
_IMPERATIVE = re.compile(
    r"^\s*(always|never|always remember|make sure|be sure|remember to|"
    r"don't forget|do not forget|when working with|if you)\b",
    re.IGNORECASE,
)


def _text_of(item: Any) -> str:
    if isinstance(item, str):
        return item
    for attr in ("text", "output"):
        v = getattr(item, attr, None)
        if v:
            return str(v)
    return ""


def _blocks(transcript: Iterable[Any]) -> list[Any]:
    out: list[Any] = []
    for item in transcript:
        blks = getattr(item, "blocks", None)
        if isinstance(blks, (list, tuple)):
            out.extend(blks)
    return out


def _bucketed(sentence: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", sentence.lower()).strip()[:120]


def _split_sentences(text: str) -> list[str]:
    if not text:
        return []
    return [s.strip() for s in _SENTENCE_RE.split(text) if s and s.strip()]


async def candidates_from_task(
    task: TaskSpec | Any,
    transcript: Iterable[Any],
    result: TaskResult | None,
) -> list[MemoryRecord]:
    """Propose memory candidates from a completed task without fabricating facts.

    This does NOT import the transcript as memory (BUILDSPEC 64, BHV-099). It
    only *proposes* candidates from declarative assistant sentences; every
    returned candidate is flagged ``promotion=required`` so nothing becomes
    durable memory without deliberate promotion. Heuristics are conservative
    and safe: subjective, imperative, and ambiguous statements are skipped.
    """
    task_id = getattr(task, "id", None) or str(task or "task")
    session_id = getattr(task, "session_id", None)

    texts: list[str] = []
    if transcript is not None:
        items = list(transcript) if not isinstance(transcript, str) else [str(transcript)]
        for item in items:
            t = _text_of(item)
            if t:
                texts.append(t)
        for blk in _blocks(items):
            t = _text_of(blk)
            if t:
                texts.append(t)

    lessons = _extract_lessons(task_id=task_id, texts=texts)

    status = getattr(result, "status", None) if result is not None else None
    objective = getattr(task, "objective", None) or ""
    episodic = MemoryRecord(
        id=new_memory_id(MemoryKind.EPISODIC),
        kind=MemoryKind.EPISODIC,
        scope=MemoryScope.TASK,
        content=f"completed task: {objective} (status={status or 'unknown'})",
        summary="episodic record of completed task",
        source=Provenance(
            source_type=SourceType.TASK,
            source_id=task_id,
            scope=MemoryScope.TASK.value,
        ),
        trust=TrustClass.AGENT_CURATED,
        created_at=utcnow(),
        metadata={
            "promotion": "required",
            "origin": "episodic",
            "task_id": task_id,
            "session_id": session_id,
            "status": getattr(result, "status", None),
        },
    )
    return [episodic, *lessons]


def _extract_lessons(*, task_id: str, texts: list[str]) -> list[MemoryRecord]:
    records: list[MemoryRecord] = []
    seen: set[str] = set()
    for text in texts or []:
        for sentence in _split_sentences(text):
            if not (12 <= len(sentence) <= 400):
                continue
            if _IMPERATIVE.match(sentence) or _SUBJECTIVE.search(sentence):
                continue
            key = _bucketed(sentence)
            if not key or key in seen:
                continue
            seen.add(key)
            records.append(
                MemoryRecord(
                    id=new_memory_id(MemoryKind.SEMANTIC),
                    kind=MemoryKind.SEMANTIC,
                    scope=MemoryScope.PROJECT,
                    content=sentence,
                    summary=f"lesson candidate from task {task_id}",
                    source=Provenance(
                        source_type=SourceType.TASK,
                        source_id=task_id,
                        scope=MemoryScope.PROJECT.value,
                    ),
                    trust=TrustClass.AGENT_CURATED,
                    created_at=utcnow(),
                    metadata={"promotion": "required", "candidate_type": MemoryKind.SEMANTIC.value},
                )
            )
    return records


__all__ = ["candidates_from_task"]
