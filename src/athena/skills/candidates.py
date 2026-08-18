"""Skill self-improvement seeding (SPEC 26, BUILDSPEC 68).

Proposes new :class:`SkillCandidate` drafts from successful task transcripts.
Candidates are drafts only — they are never auto-written or auto-promoted
(BHV-107). A follow-on validation + explicit promotion (see :mod:`lifecycle`)
decides whether they become active skills.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from athena.skills.models import Skill, SkillCandidate

_EXECUTION_MARKERS = (
    "capability_call",
    "CapabilityCallBlock",
    "capability_call:",  # rendered Message.text() copies carry this prefix
    "stdout",
    "exit_code",
    "execution ",
)

_OBJECTIVE_TOKEN_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,}")
_PROCESS_RE = re.compile(
    r"(?is)(^|\n)\s*(steps?|procedure|recipe|how to|reusable|repeat)\b"
)


def _transcript_text(items: Sequence[Any]) -> str:
    parts: list[str] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, str):
            parts.append(item)
            continue
        for getter in ("text", "output"):
            if hasattr(item, getter):
                try:
                    value = getattr(item, getter)
                    if callable(value):
                        value = value()
                except Exception:
                    continue
                if isinstance(value, str):
                    parts.append(value)
                    break
    return "\n".join(parts)


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", text.strip().lower()).strip("-")
    return cleaned[:64] or "reusable-procedure"


def _objective_of(task: Any) -> str:
    if isinstance(task, str):
        return task.strip()
    for attr in ("objective", "goal", "prompt", "description"):
        value = getattr(task, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _task_id_of(task: Any) -> str:
    if isinstance(task, str):
        return "task_proposed"
    for attr in ("task_id", "id"):
        value = getattr(task, attr, None)
        if value:
            return str(value)
    return "task_proposed"


def _objective_tokens(objective: str) -> list[str]:
    return _OBJECTIVE_TOKEN_RE.findall(objective.lower())


def _triggers(objective: str) -> tuple[str, ...]:
    tokens = [t for t in _objective_tokens(objective) if len(t) >= 2]
    return tuple(tokens[:5])


def _confidence(transcript: Sequence[Any], result: Any) -> float:
    items = list(transcript)
    if result is not None:
        items.append(result)
    if not items:
        return 0.1
    length = len(items)
    base = min(0.8, 0.3 + length * 0.05)
    markers = sum(_marker_score(item) for item in items)
    return min(0.98, round(base + markers * 0.05, 2))


def _marker_score(item: Any) -> float:
    text = _transcript_text([item])
    if not text:
        return 0.0
    score = 0.0
    if any(m in text for m in _EXECUTION_MARKERS):
        score += 0.5
    if _PROCESS_RE.search(text):
        score += 0.3
    return score


def _render_body(objective: str, transcript: Sequence[Any]) -> str:
    body = (
        "# " + _slug(objective) + "\n\n"
        "> Proposed by Athena from task self-improvement. Guidance only; it "
        "does not execute code beyond your normal capabilities.\n\n"
        "## Objective\n"
        "Follow this procedure when the task matches:\n\n"
        "> " + objective.strip() + "\n\n"
        "## Procedure\n"
        "1. Identify the concrete inputs for the task.\n"
        "2. Apply the worked procedure recorded below, adapting to context.\n"
        "3. Verify the outcome before finishing.\n"
    )
    recorded = _transcript_text(transcript)
    if recorded.strip():
        body += "\n## Prior worked transcript (evidence)\n\n" + recorded.strip() + "\n"
    return body


async def candidates_from_task(
    task: Any,
    transcript: Sequence[Any],
    result: Any = None,
    *,
    min_confidence: float = 0.4,
) -> list[SkillCandidate]:
    """Propose candidate skill drafts from a completed task transcript.

    Heuristic: if the transcript exhibits a worked, repeatable procedure
    (execution + process markers), it proposes a single draft. Returns an empty
    list when there is nothing reusable.
    """
    objective = _objective_of(task)
    if not objective:
        return []

    confidence = _confidence(transcript, result)
    if confidence < min_confidence:
        return []

    draft = Skill(
        id="",
        name=_slug(objective),
        description=f"Reusable procedure derived from: {objective[:140].strip()}",
        body=_render_body(objective, transcript),
        triggers=_triggers(objective),
        scope="user",
        version=1,
        source=None,
        enabled=True,
        metadata={"provenance": "candidate"},
    )
    candidate = SkillCandidate(
        draft=draft,
        source_task_id=_task_id_of(task),
        target_skill=None,
        rationale="Task transcript exhibited a repeatable procedure worth "
        "preserving as agent-curated guidance.",
        evidence=tuple(_evidence_refs(transcript)),
        confidence=confidence,
    )
    return [candidate]


def _evidence_refs(transcript: Sequence[Any]) -> list[str]:
    refs: list[str] = []
    for item in transcript[:5]:
        ref = getattr(item, "id", None)
        if ref is None:
            ref = getattr(item, "call_id", None)
        if ref:
            refs.append(str(ref))
    return refs


__all__ = ["SkillCandidate", "candidates_from_task"]