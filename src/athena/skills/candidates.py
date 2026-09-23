"""Skill self-improvement seeding (SPEC 26, BUILDSPEC 68).

Proposes new :class:`SkillCandidate` drafts from successful task transcripts.
Candidates are drafts only — they are never auto-written or auto-promoted
(BHV-107). A follow-on validation + explicit promotion (see :mod:`lifecycle`)
decides whether they become active skills.
"""

from __future__ import annotations

import re
import json
from dataclasses import replace
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
_PROCESS_RE = re.compile(r"(?is)(^|\n)\s*(steps?|procedure|recipe|how to|reusable|repeat)\b")
_TRIGGER_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "for",
        "from",
        "in",
        "into",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
)
_TRIGGER_GENERIC = frozenset(
    {
        "apply",
        "check",
        "do",
        "execute",
        "follow",
        "get",
        "handle",
        "inspect",
        "make",
        "process",
        "read",
        "run",
        "show",
        "task",
        "test",
        "use",
        "verify",
        "work",
    }
)
_TRIGGER_FILE_PARTS = frozenset(
    {
        "cfg",
        "css",
        "go",
        "html",
        "ini",
        "js",
        "json",
        "jsx",
        "lock",
        "md",
        "py",
        "rs",
        "toml",
        "ts",
        "tsx",
        "txt",
        "yaml",
        "yml",
    }
)


def _transcript_text(items: Sequence[Any]) -> str:
    parts: list[str] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, str):
            parts.append(item)
            continue
        conversation_text = getattr(item, "conversation_text", None)
        if callable(conversation_text):
            try:
                value = conversation_text()
            except Exception:
                value = ""
            if isinstance(value, str):
                parts.append(value)
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
    tokens: list[str] = []
    for token in _objective_tokens(objective):
        if (
            len(token) < 3
            or token in _TRIGGER_STOPWORDS
            or token in _TRIGGER_GENERIC
            or token in _TRIGGER_FILE_PARTS
        ):
            continue
        # Filenames, paths, UUID-like values, and task ids are evidence
        # details, not durable activation vocabulary.
        if "/" in token or "\\" in token or token.isdigit() or re.search(r"\d{2,}", token):
            continue
        if token.startswith(("task-", "call-", "run-", "msg-")):
            continue
        if token not in tokens:
            tokens.append(token)
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
        "2. Apply the distilled capability sequence below, adapting to context.\n"
        "3. Verify the outcome before finishing.\n"
    )
    evidence = _successful_ordinary_calls(transcript)
    if evidence:
        procedure = _procedure_evidence(transcript, evidence)
        body += "\n## Applicability\n\n"
        body += f"- Apply when the objective matches: {objective.strip()}\n"
        body += "- Required native capabilities: " + ", ".join(procedure["capabilities"]) + ".\n"
        body += "\n## Operational procedure\n\n"
        for step in procedure["steps"]:
            body += f"{step}\n"
        body += "\n## Expected observations\n\n"
        for observation in procedure["observations"]:
            body += f"- {observation}\n"
        body += "\n## Verification and stopping\n\n"
        body += "- " + procedure["verification"] + "\n"
        body += "- " + procedure["stopping"] + "\n"
        body += (
            "\nThe original transcript and result records remain separate evidence for review.\n"
        )
    return body


async def candidates_from_task(
    task: Any,
    transcript: Sequence[Any],
    result: Any = None,
    *,
    min_confidence: float = 0.4,
    target_skill: Skill | None = None,
) -> list[SkillCandidate]:
    """Propose candidate skill drafts from a completed task transcript.

    Heuristic: if the transcript exhibits a worked, repeatable procedure
    (execution + process markers), it proposes a single draft. Returns an empty
    list when there is nothing reusable.
    """
    objective = _objective_of(task)
    if not objective or _is_ephemeral_objective(objective):
        return []

    evidence = _successful_ordinary_calls(transcript)
    if _successful_excluded_calls(transcript):
        # Task-local synthesis/workflow/delegation is not portable prose. The
        # workflow learner owns deterministic native-call sequences; until an
        # excluded dependency is promoted or replaced, retain only the task
        # evidence and do not emit an incomplete skill.
        return []
    if not _repeatable_procedure_evidence(transcript, evidence, objective=objective):
        return []

    confidence = _confidence(transcript, result)
    if confidence < min_confidence:
        return []

    procedure = _procedure_evidence(transcript, evidence)
    status = getattr(getattr(result, "status", None), "value", getattr(result, "status", None))
    task_status = str(status or "unknown")
    verification_receipts = getattr(result, "verification_receipts", None)
    if verification_receipts is None and isinstance(result, dict):
        verification_receipts = result.get("verification_receipts")
    semantic_status = (
        "verified" if task_status == "COMPLETE" and verification_receipts else "unverified"
    )
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
        metadata={
            "provenance": "candidate",
            "athena": {
                "proposal_confidence": confidence,
                "evidence": {
                    "task_status": task_status,
                    "verification_receipts": verification_receipts or [],
                    "semantic_status": semantic_status,
                },
                "procedure": procedure,
            },
        },
    )
    candidate = SkillCandidate(
        draft=draft,
        source_task_id=_task_id_of(task),
        target_skill=getattr(target_skill, "id", None),
        rationale=(
            "Observed failure while using an existing skill; propose a targeted "
            "successor that preserves the prior procedure and addresses the new evidence."
            if target_skill is not None
            else "Task transcript exhibited a repeatable procedure worth preserving as "
            "agent-curated guidance."
        ),
        evidence=tuple(_evidence_refs(transcript)),
        confidence=confidence,
        target_skill_version=(
            int(getattr(target_skill, "version", 1) or 1) if target_skill is not None else None
        ),
    )
    if target_skill is not None:
        draft = replace(
            draft,
            id=target_skill.id,
            name=target_skill.name,
            version=target_skill.version,
            metadata={
                **dict(draft.metadata),
                "athena": {
                    **dict((draft.metadata.get("athena") or {})),
                    "refinement_of": {
                        "skill_id": target_skill.id,
                        "version": target_skill.version,
                    },
                },
            },
        )
        candidate = replace(candidate, draft=draft)
    return [candidate]


def _is_ephemeral_objective(objective: str) -> bool:
    return bool(
        re.fullmatch(
            r"\s*(?:hi|hello|hey|yo|thanks|thank you|good morning|good afternoon|"
            r"good evening|goodbye|bye|how are you|what(?:'s| is) up)[!.? ]*",
            objective,
            re.IGNORECASE,
        )
    )


def _successful_ordinary_calls(transcript: Sequence[Any]) -> list[Any]:
    from athena.protocol.messages import CapabilityCallBlock, CapabilityResultBlock

    calls: dict[str, Any] = {}
    results: dict[str, Any] = {}
    for item in transcript:
        for block in getattr(item, "blocks", ()):
            if isinstance(block, CapabilityCallBlock):
                calls[block.call_id] = block
            elif isinstance(block, CapabilityResultBlock):
                results[block.call_id] = block
    return [
        call
        for call in calls.values()
        if call.capability_id not in {"delegate", "workflow", "scratch", "synthesis", "capsule"}
        and getattr(results.get(call.call_id), "ok", False)
    ]


def _successful_excluded_calls(transcript: Sequence[Any]) -> list[Any]:
    from athena.protocol.messages import CapabilityCallBlock, CapabilityResultBlock

    excluded = {"delegate", "workflow", "scratch", "synthesis", "capsule"}
    calls: dict[str, Any] = {}
    results: dict[str, Any] = {}
    for item in transcript:
        for block in getattr(item, "blocks", ()):
            if isinstance(block, CapabilityCallBlock) and block.capability_id in excluded:
                calls[block.call_id] = block
            elif isinstance(block, CapabilityResultBlock):
                results[block.call_id] = block
    return [call for call in calls.values() if getattr(results.get(call.call_id), "ok", False)]


def _procedure_evidence(transcript: Sequence[Any], calls: Sequence[Any]) -> dict[str, Any]:
    from athena.protocol.messages import CapabilityResultBlock

    results: dict[str, Any] = {}
    text = _transcript_text(transcript).casefold()
    for item in transcript:
        for block in getattr(item, "blocks", ()):
            if isinstance(block, CapabilityResultBlock) and block.ok:
                results[block.call_id] = block
    steps: list[str] = []
    observations: list[str] = []
    capabilities: list[str] = []
    for index, call in enumerate(calls, 1):
        arguments = dict(getattr(call, "arguments", {}) or {})
        operation = str(arguments.get("operation") or "call")
        keys = sorted(str(key) for key in arguments if key != "operation")
        capability = str(call.capability_id)
        if capability not in capabilities:
            capabilities.append(capability)
        suffix = f"; named inputs: {', '.join(keys)}" if keys else ""
        steps.append(
            f"{index}. Use `{capability}` ({operation}){suffix}; substitute task-local values."
        )
        result = results.get(getattr(call, "call_id", ""))
        shape = _output_shape(getattr(result, "output", "") if result else "")
        observations.append(
            f"`{capability}` returns a successful result"
            + (f" with shape {shape}" if shape else "")
            + "; inspect it before continuing."
        )
    verification = (
        "Confirm the final result against the task's acceptance criteria and any "
        "independent test/check output."
        if any(marker in text for marker in ("verify", "validated", "passed", "test", "check"))
        else "Run an independent check of the intended outcome before reporting success."
    )
    return {
        "capabilities": capabilities,
        "steps": steps,
        "observations": observations,
        "verification": verification,
        "stopping": "Stop after the independent check passes; stop and preserve diagnostics on failure.",
    }


def _output_shape(raw: str) -> str:
    if not raw:
        return ""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return "a non-JSON result"
    if isinstance(value, dict):
        return "object keys " + ", ".join(sorted(str(key) for key in value)[:8])
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def _repeatable_procedure_evidence(
    transcript: Sequence[Any], evidence: Sequence[Any], *, objective: str = ""
) -> bool:
    if not evidence:
        return False
    text = _transcript_text(transcript).casefold()
    objective_text = objective.casefold()
    has_process = bool(_PROCESS_RE.search(text)) or any(
        marker in text or marker in objective_text
        for marker in ("procedure", "repeatable", "reusable", "steps")
    )
    has_verification = any(
        marker in text or marker in objective_text
        for marker in ("verify", "verified", "validated", "validation", "passed", "exit_code")
    )
    # Two successful calls establish a trace, not a reusable skill. An
    # explicit process signal plus an observable validation signal is required
    # before proposing a draft; all durable promotion remains separate.
    return has_process and has_verification and len(evidence) >= 1


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
