"""Neutral work-class/speculation admission policy (no implementation deps)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

_BROAD_SCOPE_PATTERNS = (
    re.compile(
        r"\b(?:across|throughout|all over)\s+(?:the\s+|this\s+|our\s+|entire\s+|whole\s+)?"
        r"(?:repo|repository|codebase|code\s+base|project|package|workspace|tree)\b"
    ),
    re.compile(
        r"\b(?:entire|whole|complete)\s+"
        r"(?:repo|repository|codebase|code\s+base|project|package|workspace|tree)\b"
    ),
    re.compile(r"\ball\s+(?:callers|tests|usages|modules|services|endpoints|files)\b"),
    re.compile(r"\b(?:repository|codebase|project|workspace)[- ]wide\b"),
)
_BROAD_WORK_PATTERNS = (
    re.compile(r"\brefactor(?:ing)?\b"),
    re.compile(r"\b(?:subsystem|architecture|module\s+boundary)\b"),
    re.compile(
        r"\b(?:migration|migrate|schema(?:\s+change)?|dependency|dependencies|"
        r"api\s+integration|integrate)\b"
    ),
    re.compile(
        r"\b(?:fix|repair|change|update).+\band\s+(?:run|execute|verify)\s+"
        r"(?:the\s+)?(?:full\s+|whole\s+|entire\s+)?(?:tests?|suite|checks?)\b"
    ),
)
_MUTATION_PATTERNS = (
    re.compile(
        r"\b(?:implement|add|build|create|introduce|migrate|upgrade|integrate|"
        r"redesign|rewrite|refactor)\b"
    ),
    re.compile(r"\b(?:fix|repair|correct|update|rename|adjust|change|edit|patch)\b"),
    re.compile(r"\b(?:write|remove|delete|cleanup)\b"),
)
_QUESTION_PATTERN = re.compile(
    r"^(?:how|what|why|when|where|who|which|can|could|should|is|are|do|does|did)\b"
    r"|\?\s*$"
)
_CREATIVE_PATTERN = re.compile(
    r"\b(?:poem|story|song|lyrics|essay|summary|outline|translation|translate)\b"
)
_SIMPLE_BOUNDARY_PATTERNS = (
    re.compile(r"\btypo\b"),
    re.compile(r"\bwording\b"),
    re.compile(r"\bone[- ]line(?:\s+(?:change|edit|fix))?\b"),
    re.compile(r"\bone\s+explicitly\s+named\s+file\b"),
    re.compile(r"\bone\s+(?:configuration|config)\s+value\b"),
    re.compile(r"\bone\s+symbol\s+rename(?:\s+with\s+no\s+cross[- ]file\s+implications)?\b"),
)


class WorkClass(Enum):
    NON_CODING = "non_coding"
    SIMPLE_EDIT = "simple_edit"
    COMPLEX_CODING = "complex_coding"

    @classmethod
    def of(cls, objective: str) -> "WorkClass":
        """Two-axis classification: broad scope dominates narrow wording."""
        text = " ".join(str(objective or "").casefold().split())
        if not text:
            return cls.NON_CODING

        broadness = any(
            pattern.search(text) for pattern in (*_BROAD_SCOPE_PATTERNS, *_BROAD_WORK_PATTERNS)
        )
        narrowness = any(pattern.search(text) for pattern in _SIMPLE_BOUNDARY_PATTERNS)
        mutation_intent = any(pattern.search(text) for pattern in _MUTATION_PATTERNS)

        if not mutation_intent:
            return cls.NON_CODING
        # Questions and creative/explanatory requests may mention construction
        # words ("create a poem", "how do I create...?") but do not authorize
        # a source mutation plan.
        if _QUESTION_PATTERN.search(text) or _CREATIVE_PATTERN.search(text):
            return cls.NON_CODING
        # Task directives with technical-control vocabulary (CLARIFY, FSROUND,
        # scenario/test identifiers) are runtime intents, not source-edit
        # requests.  They still pass ordinary capability policy.
        if re.search(r"\b[A-Z][A-Z0-9]*(?:[_-][A-Z0-9]+)+\b", str(objective)):
            return cls.NON_CODING
        # Runtime directives often carry a single file target.  A bounded
        # path operand alone is not repository-wide source work.
        if (
            re.search(
                r"\b(?:file|path|config(?:uration)?|settings?)(?:\s+\S+){0,3}$",
                text,
            )
            and not broadness
        ):
            return cls.SIMPLE_EDIT
        # A dot-suffixed file operand is a bounded data/workspace target.
        if (
            re.search(r"\b[\w-]+\.[A-Za-z0-9]{1,8}\b", text)
            and not broadness
            and not re.search(r"\b(?:src|source|lib|package|module)s?(?:/|\s)", text)
        ):
            return cls.SIMPLE_EDIT
        if broadness:
            return cls.COMPLEX_CODING
        if narrowness:
            return cls.SIMPLE_EDIT
        return cls.COMPLEX_CODING


class SpeculationDepth(Enum):
    NONE = "none"
    SINGLE_CANDIDATE = "single_candidate"
    MULTI_CANDIDATE = "multi_candidate"


@dataclass(frozen=True)
class SpeculationDecision:
    work_class: WorkClass
    depth: SpeculationDepth

    def to_record(self) -> dict[str, str]:
        return {
            "work_class": self.work_class.value,
            "speculation_depth": self.depth.value,
        }


def decide_speculation(
    objective: str,
    *,
    verification_failures: int = 0,
    explicit_comparison: bool = False,
) -> SpeculationDecision:
    """Return the default admission decision for an objective.

    Most complex coding work needs one sticky candidate. Multi-candidate is
    exceptional and bounded: it is promoted only by deterministic facts such
    as repeated verification failure or an explicit comparison request, never
    inferred from prose complexity alone.
    """
    work_class = WorkClass.of(objective)
    depth = SpeculationDepth.NONE
    if work_class is WorkClass.COMPLEX_CODING:
        depth = SpeculationDepth.SINGLE_CANDIDATE
    if explicit_comparison and work_class is WorkClass.COMPLEX_CODING:
        depth = SpeculationDepth.MULTI_CANDIDATE
    elif verification_failures >= 2 and work_class is WorkClass.COMPLEX_CODING:
        depth = SpeculationDepth.MULTI_CANDIDATE
    return SpeculationDecision(work_class=work_class, depth=depth)


__all__ = ["SpeculationDecision", "SpeculationDepth", "WorkClass", "decide_speculation"]
