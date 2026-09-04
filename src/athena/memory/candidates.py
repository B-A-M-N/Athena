from __future__ import annotations

import re
from typing import Any, Iterable

from athena.protocol.memory import MemoryKind, MemoryRecord, MemoryScope
from athena.protocol.messages import (
    CapabilityResultBlock,
    Message,
    ReasoningBlock,
    Role,
    TextBlock,
    Provenance,
    SourceType,
    TrustClass,
    utcnow,
)
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
_EXPLICIT_FACT = re.compile(
    r"\b(i am|i'm|i use|i prefer|my |call me|remember that|the .+ is)\b",
    re.IGNORECASE,
)
_EPHEMERAL = re.compile(
    r"^(?:hi|hello|hey|yo|thanks|thank you|good morning|good afternoon|"
    r"good evening|goodbye|bye|how are you|what(?:'s| is) up)[!.? ]*$",
    re.IGNORECASE,
)


def _text_of(item: Any) -> str:
    if isinstance(item, str):
        return item
    for attr in ("text", "output"):
        v = getattr(item, attr, None)
        if callable(v):
            try:
                v = v()
            except Exception:
                continue
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
    explicit_facts: list[str] = []
    successful_observations: list[str] = []
    if transcript is not None:
        items = list(transcript) if not isinstance(transcript, str) else [str(transcript)]
        for item in items:
            if isinstance(item, str):
                if _EXPLICIT_FACT.search(item):
                    explicit_facts.append(item)
                else:
                    texts.append(item)
                continue
            item_blocks = getattr(item, "blocks", None)
            if isinstance(item, Message) or isinstance(item_blocks, (list, tuple)):
                role = getattr(item, "role", None)
                role_value = getattr(role, "value", role)
                user_text: list[str] = []
                assistant_text: list[str] = []
                for block in item_blocks or ():
                    if isinstance(block, ReasoningBlock):
                        continue
                    if isinstance(block, CapabilityResultBlock):
                        if block.ok and block.output:
                            successful_observations.append(block.output)
                        continue
                    if isinstance(block, TextBlock) and block.text:
                        if role is Role.USER or role_value == Role.USER.value:
                            user_text.append(block.text)
                        elif role is Role.ASSISTANT or role_value == Role.ASSISTANT.value:
                            assistant_text.append(block.text)
                for text in user_text:
                    if _EXPLICIT_FACT.search(text):
                        explicit_facts.append(text)
                # Explicit facts receive a user-scoped candidate below; do
                # not also pass them through generic lesson extraction.
                # Assistant prose is semantic evidence only when the same
                # transcript contains a successful observable result. It is
                # never inferred from hidden reasoning.
                if successful_observations:
                    texts.extend(assistant_text)
                texts.extend(successful_observations)
                successful_observations.clear()
                continue
            # Legacy/raw transcript fixtures have no role contract. Preserve
            # their explicit text while structured messages use the gates above.
            t = _text_of(item)
            if t:
                texts.append(t)

    lessons = _extract_lessons(task_id=task_id, texts=texts)
    lessons.extend(
        _explicit_fact_records(
            task_id=task_id,
            session_id=session_id,
            texts=explicit_facts,
        )
    )

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
    if _EPHEMERAL.fullmatch(str(objective or "").strip()):
        return lessons
    return [episodic, *lessons]


def _explicit_fact_records(
    *, task_id: str, session_id: str | None, texts: list[str]
) -> list[MemoryRecord]:
    records: list[MemoryRecord] = []
    seen: set[str] = set()
    for text in texts:
        for sentence in _split_sentences(text):
            if not _EXPLICIT_FACT.search(sentence):
                continue
            key = _bucketed(sentence)
            if not key or key in seen:
                continue
            seen.add(key)
            records.append(
                MemoryRecord(
                    id=new_memory_id(MemoryKind.SEMANTIC),
                    kind=MemoryKind.SEMANTIC,
                    scope=MemoryScope.SESSION,
                    content=sentence,
                    summary="explicit user fact or preference",
                    source=Provenance(
                        source_type=SourceType.USER,
                        source_id=task_id,
                        scope=MemoryScope.SESSION.value,
                    ),
                    trust=TrustClass.USER_CONTENT,
                    created_at=utcnow(),
                    metadata={
                        "promotion": "required",
                        "candidate_type": "explicit_user_fact",
                        "scope_id": session_id,
                        "session_id": session_id,
                    },
                )
            )
    return records


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
