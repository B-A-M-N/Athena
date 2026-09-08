"""Deterministic affordance guidance for the single Athena model loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Iterable, Mapping
import re
from typing import Any

RESPONSE_ONLY = "response_only"
OBSERVABLE_WORK_REQUIRED = "observable_work_required"
RESPONSE = "response"
OBSERVATION = "observation"
EXECUTION = "execution"
MUTATION = "mutation"
EXTERNAL_ACTION = "external_action"
# A referential continuation ("go ahead", "then commit it") cannot be typed
# into one evidence category from its own text; at termination any real
# non-control work evidence satisfies it.
CONTINUATION = "continuation"
RESPOND = "respond"
ACT = "act"
DISCOVER = "discover"
GAP = "gap"

# The deterministic pre-model layer answers one narrow question: "is this
# turn *definitely* a self-contained response-only exchange?"  Anything
# ambiguous — questions about current state, files, code, logs, previous
# work, or referential continuations — stays tool-eligible and the model
# decides among the visible capabilities.
DEFINITELY_RESPONSE_ONLY = "definitely_response_only"
MAY_REQUIRE_OBSERVATION_OR_ACTION = "may_require_observation_or_action"

_ACTION_TERMS = frozenset(
    {
        "apply",
        "build",
        "calculate",
        "change",
        "check",
        "compare",
        "compute",
        "create",
        "delete",
        "edit",
        "execute",
        "fix",
        "generate",
        "inspect",
        "list",
        "move",
        "open",
        "patch",
        "read",
        "research",
        "run",
        "search",
        "show",
        "test",
        "update",
        "verify",
        "write",
    }
)
_ACTION_OBJECT_TERMS = frozenset(
    {
        "artifact",
        "artifacts",
        "app",
        "application",
        "branch",
        "command",
        "config",
        "configuration",
        "data",
        "directory",
        "diff",
        "docs",
        "documentation",
        "evidence",
        "file",
        "files",
        "folder",
        "git",
        "issue",
        "job",
        "log",
        "logs",
        "meeting",
        "message",
        "output",
        "path",
        "problem",
        "project",
        "release",
        "report",
        "repo",
        "repository",
        "result",
        "results",
        "source",
        "sources",
        "status",
        "task",
        "tests",
        "workspace",
    }
)

_RESPONSE_FRAME = re.compile(
    r"^\s*(?:how\b|what\b|why\b|explain\b|describe\b|tell me\b|"
    r"show me how\b|could you explain\b)",
    re.IGNORECASE,
)
_RESPONSE_CONDITIONAL = re.compile(r"^\s*what\s+happens?\s+(?:if|when)\b", re.IGNORECASE)
_COMPARE_RESPONSE = re.compile(r"^\s*compare\b.+\band\b", re.IGNORECASE)
_IMPERATIVE_FRAME = re.compile(
    r"^\s*(?:do|perform|build|fix|inspect|read|open|show|list|run|execute|"
    r"test|check|research|search|create|write|generate|delete|edit|patch|"
    r"change|update|apply|move|save|set|compose|schedule|send|commit)\b",
    re.IGNORECASE,
)
_IMPERATIVE_OVERRIDE = re.compile(
    r"\b(?:and do it|do that now|make the change|run it|"
    r"inspect (?:the )?actual repo|open (?:the )?file)\b",
    re.IGNORECASE,
)
_PERSISTENT_TARGET = re.compile(
    r"(?:\b(?:save|persist|overwrite|delete|remove|edit|patch|update|fix|"
    r"write|create|make|change|modify|open|inspect|read|show|list|run|test|"
    r"execute|research|schedule|send|deploy)\b[^\n]{0,180}"
    r"(?:[A-Za-z0-9_.-]+\.(?:py|js|ts|tsx|jsx|rs|go|md|txt|json|yaml|yml|toml|csv|sh)|"
    r"\b(?:file|files|folder|directory|repo|repository|workspace|branch|diff|"
    r"tests?|app|application|project|artifact|report|job|meeting|message|email|"
    r"issue|release|database|record|table|config|configuration|path)\b))",
    re.IGNORECASE,
)
# Workspace/observability vocabulary. A turn that *refers to* these is a
# question about observable state, not pure conversation — the model needs a
# tool surface to answer honestly even though the sentence is grammatically
# a question ("what changed in this repo?", "why is this test failing?").
# Strong terms almost always refer to the actual environment; weak terms
# count only inside a referential construction ("this test", "my config"),
# so instructional questions like "how do I create a Python file?" stay
# self-contained responses.
_WORKSPACE_STRONG_TERMS = frozenset(
    {
        "repo",
        "repository",
        "codebase",
        "workspace",
        "code",
        "log",
        "logs",
        "traceback",
        "stacktrace",
        "pytest",
        "readme",
        "changelog",
        "linter",
        "git",
        "diff",
        "commit",
        "branch",
        "coverage",
        "docs",
    }
)
_WORKSPACE_WEAK_TERMS = frozenset(
    {
        "file",
        "files",
        "folder",
        "directory",
        "test",
        "tests",
        "config",
        "configuration",
        "schema",
        "database",
        "build",
        "error",
        "errors",
        "crash",
        "crashed",
        "stack",
        "output",
        "assert",
        "assertion",
        "environment",
        "dependency",
        "dependencies",
        "package",
        "packages",
        "spec",
        "project",
        "setup",
    }
)
_WORKSPACE_TERMS = _WORKSPACE_STRONG_TERMS | _WORKSPACE_WEAK_TERMS
# Session-past referentials. Unlike live anaphora ("it", "that"), these
# name a point in the session's own history: what actually happened or was
# used is knowable only from observed session state, so a question frame
# carrying them requires evidence rather than prose recall.
_SESSION_PAST_TERMS = frozenset(
    {
        "last",  # "what did we use last time?"
        "previous",
        "previously",
        "earlier",
        "before",
        "yesterday",
    }
)
_REFERENTIAL_CONSTRUCTION = re.compile(
    r"\b(?:this|that|these|those|the\s+current|my|our|your)\b[^.?!]{0,40}?"
    r"\b(?:file|files|folder|directory|test|tests|config|configuration|schema|"
    r"database|build|error|errors|stack|output|environment|project|setup|"
    r"repo|repository|workspace|code|function|functions|method|methods|"
    r"module|modules|class|classes|library|libraries|package|packages)\b",
    re.IGNORECASE,
)
# Referential continuation vocabulary. These turns only make sense against
# prior conversation or observed state ("yes, do that", "continue",
# "then commit it"), so they can never be classified response-only on
# surface grammar alone.
_CONTINUATION_TERMS = frozenset(
    {
        "continue",
        "again",
        "retry",
        "proceed",
        "it",
        "that",
        "them",
        "those",
        "this",
        "these",
        "same",
        "first",
        "second",
        "last",
        "previous",
        "other",
        "another",
        "both",
    }
)
# Bare process continuations: single words that direct the agent to keep
# working. They are tool-eligible even though they name no object.
_PROCESS_CONTINUATIONS = frozenset({"continue", "keep", "going", "proceed", "resume"})
_CONTINUATION_PHRASES = (
    "go ahead",
    "do it",
    "do that",
    "do so",
    "go on",
    "keep going",
    "carry on",
    "try again",
    "sounds good",
    "yes",
    "yeah",
    "yep",
    "ok",
    "okay",
    "sure",
    "please do",
    "commit it",
    "commit that",
    "run it",
    "fix it",
    "test it",
    "ship it",
    "use it",
    "same thing",
    "same setup",
    "as before",
    "as usual",
    "the way we",
    "that one",
    "first one",
    "second one",
)
_TARGET_TERMS = frozenset(
    {
        "artifact",
        "artifacts",
        "app",
        "application",
        "branch",
        "command",
        "config",
        "configuration",
        "data",
        "directory",
        "diff",
        "docs",
        "documentation",
        "file",
        "files",
        "folder",
        "git",
        "issue",
        "job",
        "log",
        "logs",
        "meeting",
        "message",
        "output",
        "path",
        "problem",
        "project",
        "release",
        "report",
        "repo",
        "repository",
        "result",
        "results",
        "source",
        "sources",
        "status",
        "task",
        "test",
        "tests",
        "workspace",
        "workflow",
        "pipeline",
        "database",
        "record",
        "table",
        "email",
        "evidence",
        # Code-unit nouns: a referential construction naming one ("this
        # function", "that method") points at observable source state.
        "function",
        "functions",
        "method",
        "methods",
        "module",
        "modules",
        "class",
        "classes",
        "library",
        "libraries",
        "package",
        "packages",
    }
)


@dataclass(frozen=True)
class TurnIntent:
    """The deterministic frame of a user turn.

    This is deliberately a small classifier, not an execution planner.  Its
    authoritative output is the narrow binary in ``channel``: either the turn
    is *definitely* a self-contained response-only exchange, or it may require
    observation or action and stays tool-eligible.  The fine-grained ``kind``
    remains as advisory strategy metadata; it is no longer authority over
    whether the model may call capabilities.
    """

    kind: str
    requires_observable_work: bool
    rationale: str
    channel: str = MAY_REQUIRE_OBSERVATION_OR_ACTION


def resolve_turn_intent(objective: str) -> TurnIntent:
    raw = str(objective or "").strip()
    text = raw.casefold()
    terms = set(_tokens(text))

    # ---- Definite response-only channel ------------------------------ #
    # Only turns that are self-contained *without* any observable state keep
    # tools suppressed.  Everything ambiguous falls through to the eligible
    # channel — workspace questions and continuation language must never be
    # answerable "from nothing".
    if _is_definitely_response_only(raw, text, terms):
        return TurnIntent(
            RESPONSE,
            False,
            "self-contained conversational response; no observable target named",
            channel=DEFINITELY_RESPONSE_ONLY,
        )

    # ---- Advisory fine-grained classification ------------------------ #
    # This branch order is unchanged in spirit: it still produces the typed
    # OBSERVATION / EXECUTION / MUTATION / EXTERNAL_ACTION kinds used as
    # advisory metadata and as the expected evidence category at termination.
    # It no longer gates tool eligibility — that is the narrow channel above.
    # Fixture/label-like identifiers are not commands by themselves.
    if re.fullmatch(r"[A-Z0-9][A-Z0-9_ -]*", raw) and "_" in raw:
        return TurnIntent(
            RESPONSE,
            False,
            "identifier-like prompt without an action frame",
        )
    if _IMPERATIVE_OVERRIDE.search(text):
        return _intent_from_action_terms(terms, target=True)
    if (
        _RESPONSE_FRAME.search(raw)
        or _RESPONSE_CONDITIONAL.search(raw)
        or _COMPARE_RESPONSE.search(raw)
    ):
        # Two separate questions (P0): may the turn use tools, and does an
        # honest answer require observed evidence? A question framed about
        # observable workspace state ("what changed in this repo?") keeps
        # its advisory RESPONSE kind but REQUIRES evidence — answering from
        # invented prose must not satisfy completion. Hypotheticals ("what
        # happens if…") are knowledge questions and stay response-only.
        if _HYPOTHETICAL.search(raw) or not _names_observable_state(raw, terms):
            return TurnIntent(RESPONSE, False, "explanatory or interrogative response frame")
        return TurnIntent(
            RESPONSE,
            True,
            "question about observable workspace state; answering honestly requires evidence",
        )

    creative = terms & {"poem", "poetry", "story", "haiku", "names", "name", "joke", "slogan"}
    if (
        creative
        and terms & {"create", "generate", "write", "make"}
        and not terms
        & {"file", "files", "save", "persist", "repo", "repository", "workspace", "artifact"}
    ):
        return TurnIntent(RESPONSE, False, "creative generation is a response artifact")

    target = bool(_PERSISTENT_TARGET.search(raw) or terms & _TARGET_TERMS)
    if terms & {"run", "execute", "exec", "pytest", "test", "tests"} and not terms & {
        "fix",
        "edit",
        "patch",
        "write",
        "create",
        "delete",
        "change",
        "modify",
        "update",
    }:
        # A question such as "what happens when I run pytest?" was handled by
        # the response frame above; bare imperatives are execution requests.
        return TurnIntent(EXECUTION, True, "explicit execution/test frame")
    if terms & {"send", "schedule", "notify", "email", "deploy", "publish"} and target:
        return TurnIntent(EXTERNAL_ACTION, True, "explicit external side-effect frame")
    if (
        terms
        & {
            "create",
            "delete",
            "remove",
            "edit",
            "fix",
            "patch",
            "write",
            "save",
            "persist",
            "change",
            "modify",
            "update",
            "apply",
            "build",
            "make",
            "move",
            "set",
            "compose",
            "commit",
        }
        and target
    ):
        return TurnIntent(MUTATION, True, "explicit persistent mutation frame")
    if "set" in terms and "then" in terms:
        if terms & {"read", "inspect", "show", "check"}:
            return TurnIntent(EXECUTION, True, "sequenced state execution frame")
        return TurnIntent(MUTATION, True, "sequenced state mutation frame")
    if (
        terms
        & {
            "read",
            "inspect",
            "open",
            "show",
            "list",
            "check",
            "view",
            "research",
            "search",
            "compare",
        }
        and target
    ):
        return TurnIntent(OBSERVATION, True, "explicit observation frame")

    if _IMPERATIVE_FRAME.search(raw):
        if terms & {
            "create",
            "delete",
            "remove",
            "edit",
            "fix",
            "patch",
            "write",
            "save",
            "persist",
            "change",
            "modify",
            "update",
            "apply",
            "build",
            "make",
            "move",
            "set",
            "compose",
            "commit",
        }:
            return TurnIntent(MUTATION, True, "imperative mutation frame")
        if terms & {"run", "execute", "exec", "pytest", "test", "tests"}:
            return TurnIntent(EXECUTION, True, "imperative execution frame")
        return TurnIntent(OBSERVATION, True, "imperative observation frame")

    # A referential continuation ("go ahead", "yes, do that") is
    # tool-eligible by construction, but it names no typed evidence category
    # of its own — the expected category is resolved against actual work
    # evidence at termination instead of from this sentence.
    if _is_continuation(text, terms):
        return TurnIntent(
            CONTINUATION,
            True,
            "referential continuation; expected work resolves against observed evidence",
        )

    # A turn that names observable state is evidence-bearing even when no
    # action frame matched ("look at the logs and tell me why it crashed"):
    # the advisory kind stays RESPONSE but completion requires observation.
    if _names_observable_state(raw, terms):
        return TurnIntent(
            RESPONSE,
            True,
            "turn refers to observable workspace state; evidence required",
        )

    # Creative and computational generation is normally answered in the
    # response channel. It becomes observable work only when a persistent
    # target was named (for example, "generate report.md").
    return TurnIntent(RESPONSE, False, "no persistent or external target")


def is_explicit_response_turn(objective: str) -> bool:
    """Return whether the turn is *definitely* a self-contained response.

    This is the narrow authority boundary: greetings, thanks, small talk and
    other self-contained conversation can suppress the tool surface.  Any
    turn that references workspace state, prior work, or a continuation stays
    tool-eligible — surface grammar like a leading "what" or "why" is never
    authority on its own.
    """
    return resolve_turn_intent(objective).channel == DEFINITELY_RESPONSE_ONLY


def _names_observable_state(raw: str, terms: set[str]) -> bool:
    """Whether a turn refers to observable workspace/environment state.

    Shared by the tool-eligibility channel and the evidence-requirement
    classifier so the two boundaries cannot drift apart: a turn this
    predicate flags is both tool-eligible AND required to ground its answer
    in observed evidence. Strong environment vocabulary always refers to
    observable state; generic targets and weak workspace words count only
    inside a referential construction ("this file", "my config").
    """
    if _PERSISTENT_TARGET.search(raw) and _REFERENTIAL_CONSTRUCTION.search(raw):
        return True
    if terms & _WORKSPACE_STRONG_TERMS:
        return True
    if _REFERENTIAL_CONSTRUCTION.search(raw) and terms & (_TARGET_TERMS | _WORKSPACE_WEAK_TERMS):
        return True
    if terms & _SESSION_PAST_TERMS:
        return True
    return False


def _is_definitely_response_only(raw: str, text: str, terms: set[str]) -> bool:
    """Decide the narrow channel: definitely response-only, or eligible.

    A turn qualifies only when it (a) carries conversational framing, and
    (b) names no observable target, no continuation, and no workspace state.
    Questions about files, repos, logs, tests, or prior turns stay eligible.
    """
    conversational = bool(
        _RESPONSE_FRAME.search(raw)
        or _RESPONSE_CONDITIONAL.search(raw)
        or _COMPARE_RESPONSE.search(raw)
        or _QUESTION_FRAME.search(raw)
        or terms
        & {
            "hi",
            "hello",
            "hey",
            "thanks",
            "thank",
            "goodbye",
            "bye",
            "joke",
            "poem",
            "poetry",
            "story",
            "haiku",
            "recursion",
        }
        or _SMALL_TALK.search(text)
    )
    if not conversational:
        return False
    # A hypothetical conditional asks what *would* happen; it is answered
    # from knowledge, not from observable state, whatever it mentions.
    if _HYPOTHETICAL.search(raw):
        return True
    if _names_observable_state(raw, terms):
        return False
    # Any continuation vocabulary ("last time", "previous", "again") keeps
    # the turn referential to prior conversation or work, never response-only.
    if terms & _CONTINUATION_TERMS:
        return False
    if _is_continuation(text, terms):
        return False
    # "Could you take a look at the code?" / "Can you help me debug this
    # repository?" carry request framing that targets observable state.
    if _REQUEST_HELP.search(raw):
        return False
    return True


def _is_continuation(text: str, terms: set[str]) -> bool:
    """Detect referential continuation language ("go ahead", "then commit it")."""
    normalized = " " + " ".join(_tokens(text)) + " "
    for phrase in _CONTINUATION_PHRASES:
        if f" {phrase} " in normalized:
            return True
    # A bare process continuation ("continue", "keep going") directs the
    # agent to resume work; it is never answerable from nothing.
    if terms and terms <= _PROCESS_CONTINUATIONS:
        return True
    # Bare affirmation plus any verb-ish token ("yes, patch", "sure apply").
    if terms & {"yes", "yeah", "yep", "ok", "okay", "sure", "please"} and len(terms) > 1:
        return True
    return False


_SMALL_TALK = re.compile(
    r"^\s*(?:how(?:'s| is| are)\s+you\b|what'?s\s+up\b|good\s+(?:morning|afternoon|evening)\b|"
    r"tell\s+me\s+a\s+(?:joke|story|poem)\b|who\s+are\s+you\b|what\s+are\s+you\b)",
    re.IGNORECASE,
)
# A hypothetical conditional ("what happens if…", "what would happen when…")
# asks about general behavior, not the current observable environment.
_HYPOTHETICAL = re.compile(
    r"^\s*what\s+(?:would|will|happens?\s+(?:if|when))",
    re.IGNORECASE,
)
# Generic interrogatives ("who wrote Hamlet?", "when did X happen?") are
# general-knowledge questions, not references to observable environment.
_QUESTION_FRAME = re.compile(
    r"^\s*(?:who|when|where)\b",
    re.IGNORECASE,
)
_REQUEST_HELP = re.compile(
    r"\b(?:take a look|look at|check out|help me|debug|inspect|review|examine)\b",
    re.IGNORECASE,
)


def _intent_from_action_terms(terms: set[str], *, target: bool) -> TurnIntent:
    if terms & {"run", "execute", "exec", "pytest", "test", "tests"}:
        return TurnIntent(EXECUTION, True, "imperative execution override")
    if terms & {"send", "schedule", "notify", "email", "deploy", "publish"}:
        return TurnIntent(EXTERNAL_ACTION, True, "imperative external-action override")
    if target or terms & {
        "fix",
        "edit",
        "patch",
        "write",
        "create",
        "delete",
        "change",
        "make",
        "set",
        "compose",
    }:
        return TurnIntent(MUTATION, True, "imperative mutation override")
    return TurnIntent(OBSERVATION, True, "imperative observation override")


@dataclass(frozen=True)
class StrategyAffordance:
    """Facts about one affordance visible to the advisory selector."""

    id: str
    description: str = ""
    available: bool = True
    scope: str = "system"
    dependency_ready: bool = True
    environment_compatible: bool = True
    proof: Mapping[str, Any] = field(default_factory=dict)
    effects: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    output_schema: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "description": self.description,
            "available": self.available,
            "scope": self.scope,
            "dependency_ready": self.dependency_ready,
            "environment_compatible": self.environment_compatible,
            "proof": dict(self.proof),
            "effects": self.effects,
            "tags": self.tags,
            "output_schema": dict(self.output_schema),
        }


@dataclass(frozen=True)
class StrategyGuidance:
    """A small, model-visible hint—not a second planner or execution path."""

    route: str
    rationale: str
    candidates: tuple[str, ...] = ()
    missing_affordance: str | None = None
    gap_kind: str | None = None
    route_kind: str = "existing_primitive"
    affordances: tuple[StrategyAffordance, ...] = ()
    # These fields make the boundary explicit without giving strategy a
    # second planning loop. ``route`` remains a compatibility label for
    # existing callers; ``decision`` is the small behavioral contract used by
    # the kernel and model prompt.
    decision: str = RESPOND
    completion_mode: str = RESPONSE_ONLY
    discovery_state: str = "not_required"
    turn_intent: str = RESPONSE

    def to_dict(self) -> dict[str, object]:
        return {
            "route": self.route,
            "rationale": self.rationale,
            "candidates": self.candidates,
            "missing_affordance": self.missing_affordance,
            "gap_kind": self.gap_kind,
            "route_kind": self.route_kind,
            "affordances": tuple(item.to_dict() for item in self.affordances),
            "decision": self.decision,
            "completion_mode": self.completion_mode,
            "discovery_state": self.discovery_state,
            "turn_intent": self.turn_intent,
        }


def select_strategy(
    objective: str,
    capability_ids: Iterable[str | Mapping[str, Any] | Any],
    *,
    discovery_state: str | None = None,
    missing_affordance: str | None = None,
    require_tools: bool = False,
) -> StrategyGuidance:
    """Select bounded advisory guidance from visible affordance evidence.

    The model still chooses the actual calls. This function only makes the
    existing architecture explicit and observable. Callers may provide legacy
    ids, descriptors, or fabric search records; all are normalized into the
    same typed evidence before selection.
    """
    text = str(objective or "").casefold()
    intent = resolve_turn_intent(objective)
    # Only a DEFINITELY response-only turn has no capability surface at all.
    # This early return is important: discovery evidence is not harmless
    # presentation data, because exposing it changes model behavior and model
    # selection. But the advisory fine-grained ``kind`` is not the authority
    # here — the narrow channel is. A grammatical question referencing
    # workspace state ("what does this function do?") is response-kind in the
    # advisory metadata yet stays tool-eligible, so it falls through and is
    # answered from the visible affordance inventory.
    # ``require_tools`` remains the explicit caller exception for
    # providers/tests that insist on a tool-capable model even for a
    # definite response.
    if intent.channel == DEFINITELY_RESPONSE_ONLY and not require_tools:
        return StrategyGuidance(
            route=RESPOND,
            rationale=intent.rationale,
            decision=RESPOND,
            completion_mode=RESPONSE_ONLY,
            discovery_state="not_required",
            turn_intent=intent.kind,
        )
    affordances = tuple(_coerce_affordance(value) for value in capability_ids)
    available = {
        item.id
        for item in affordances
        if item.available and item.dependency_ready and item.environment_compatible
    }

    actionable = intent.requires_observable_work
    state = str(discovery_state or ("resolved" if affordances else "miss"))

    # An empty inventory is not itself proof that the objective is
    # conversational. Keep response-only, discovery miss, and degraded
    # discovery distinct so an action task cannot be silently marked done.
    if not affordances:
        state = str(discovery_state or ("miss" if actionable else "not_required"))
        if actionable and state == "gap":
            return StrategyGuidance(
                route=GAP,
                rationale="Discovery established that the required affordance is unavailable.",
                missing_affordance=missing_affordance,
                gap_kind="missing_affordance",
                route_kind="affordance_gap",
                decision=GAP,
                completion_mode=OBSERVABLE_WORK_REQUIRED,
                discovery_state=state,
                turn_intent=intent.kind,
            )
        if actionable and state in {"miss", "degraded", "unavailable"}:
            rationale = (
                "Capability discovery is degraded; inspect the available action surface before proceeding."
                if state == "degraded"
                else "No suitable existing affordance was resolved; discover or reflect before claiming completion."
            )
            return StrategyGuidance(
                route=DISCOVER,
                rationale=rationale,
                gap_kind=f"discovery_{state}",
                route_kind="affordance_discovery",
                decision=DISCOVER,
                completion_mode=OBSERVABLE_WORK_REQUIRED,
                discovery_state=state,
                turn_intent=intent.kind,
            )
        return StrategyGuidance(
            route=RESPOND,
            rationale=(
                "No observable-work signal was detected; answer this conversational turn directly."
            ),
            gap_kind="empty_inventory" if state == "not_required" else f"discovery_{state}",
            decision=RESPOND,
            completion_mode=RESPONSE_ONLY,
            discovery_state=state,
            affordances=affordances,
            turn_intent=intent.kind,
        )

    if actionable and state in {"miss", "degraded", "unavailable"}:
        discovery_candidates = tuple(
            item.id for item in affordances if _matches(item.id, "capabilities")
        )
        detail = (
            "Capability discovery is degraded; use the bounded reflection surface before proceeding."
            if state == "degraded"
            else "The initial capability search missed this action; use reflection to discover a suitable affordance."
        )
        return StrategyGuidance(
            route=DISCOVER,
            rationale=detail,
            candidates=discovery_candidates,
            route_kind="affordance_discovery",
            affordances=affordances,
            decision=DISCOVER,
            completion_mode=OBSERVABLE_WORK_REQUIRED,
            discovery_state=state,
            turn_intent=intent.kind,
        )

    # Route is advisory evidence ranking over the visible descriptor
    # inventory, declared effects/tags, and readiness proof. Profiles carry
    # no objective-keyword signals: a keyword must never summon a route or
    # manufacture a missing primitive (P1-24 removed the dead sets).
    profiles: tuple[Mapping[str, Any], ...] = (
        {
            "route": "fusion",
            "preferred": ("fusion", "workflow", "fs", "execute"),
            "tags": {"fusion", "shadow", "experiment"},
            "effects": {"execute", "write_local"},
            "rationale": "Bounded speculative work should be proven in a shadow before commit.",
        },
        {
            "route": "evidence_acquisition",
            "preferred": ("research", "workflow", "artifacts"),
            "tags": {"research", "evidence", "sources"},
            "effects": {"read_local", "network_read"},
            "rationale": "Sourced work needs bounded acquisition and explicit gap handling.",
        },
        {
            "route": "synthesize",
            "preferred": ("synthesis", "scratch", "workflow"),
            "tags": {"synthesis", "generated", "tool"},
            "effects": {"execute", "write_local"},
            "rationale": "Reusable behavior should be validated task-locally before promotion.",
        },
        {
            "route": "compose",
            "preferred": ("workflow", "execute", "fs"),
            "tags": {"workflow", "pipeline", "compose"},
            "effects": {"execute", "write_local"},
            "rationale": "Ordered work should use a bounded workflow when one exists.",
        },
        {
            "route": "direct",
            "preferred": ("capabilities", "execute", "fs", "workflow", "scratch", "synthesis"),
            "tags": {"primitive", "native"},
            "effects": {"execute", "read_local"},
            "priority": 1,
            "rationale": "Start with the smallest visible primitive; compose or build only when it is insufficient.",
        },
    )
    terms = set(_tokens(text))

    def profile_score(profile: Mapping[str, Any]) -> tuple[int, int, int, str]:
        preferred_ids = tuple(profile["preferred"])
        evidence_score = 0
        for item in affordances:
            id_match = next(
                (
                    index
                    for index, preferred in enumerate(preferred_ids)
                    if _matches(item.id, preferred)
                ),
                None,
            )
            tag_match = bool(set(item.tags) & set(profile["tags"]))
            effect_match = bool(set(item.effects) & set(profile["effects"]))
            description_match = bool(terms & set(_tokens(item.description)))
            if id_match is not None:
                # Identity is useful evidence, but only a weak prior.  A
                # route must earn its score from the declared affordance
                # surface, effects, tags, and proof rather than winning by a
                # longer list of familiar substrings.
                evidence_score += 2 if id_match == 0 else 1
            if tag_match:
                evidence_score += 3
            if effect_match:
                evidence_score += 1 if item.available else 0
            if description_match and (id_match is not None or tag_match):
                evidence_score += 1
            if item.available and (
                item.proof.get("all_passed") is True
                or item.proof.get("validation_state")
                in {
                    "VALIDATED",
                    "PROMOTED",
                }
            ):
                evidence_score += 1
        # The objective is used only to check the descriptions of already
        # visible affordances. It cannot summon a fusion/synthesis/workflow
        # route or manufacture a missing primitive from a keyword.
        return (
            evidence_score,
            evidence_score,
            int(profile.get("priority", 0)),
            profile["route"],
        )

    selected_profile = max(profiles, key=profile_score)
    preferred = tuple(selected_profile["preferred"])
    route = str(selected_profile["route"])
    rationale = str(selected_profile["rationale"])

    candidates = tuple(
        capability
        for capability in preferred
        if any(_matches(item_id, capability) for item_id in available)
    )
    if route == "direct" and not candidates:
        # A precise fabric match may be a valid primitive whose id is not in
        # the small compatibility preference list (for example ``schedule``).
        # Keep it visible as direct evidence instead of inventing a missing
        # ``capabilities`` umbrella gap.
        candidates = tuple(sorted(available))
    # The first candidate names the selected route's primary affordance.  A
    # convenient fallback must not make a materially different route look
    # equivalent to the requested one.
    preferred_record = (
        next((item for item in affordances if _matches(item.id, preferred[0])), None)
        if preferred
        else None
    )
    # Direct work can use any visible primitive in its ordered preference
    # list; the absence of the optional ``capabilities`` umbrella must not
    # turn an otherwise usable ``execute`` or ``fs`` primitive into a gap.
    primary_missing = not any(_matches(item_id, preferred[0]) for item_id in available)
    missing = (
        preferred[0]
        if preferred and primary_missing and (route != "direct" or not candidates)
        else None
    )
    if missing is not None:
        gap_kind = "missing_affordance"
        if preferred_record is not None and not preferred_record.dependency_ready:
            gap_kind = "dependency_unready"
        elif preferred_record is not None and not preferred_record.environment_compatible:
            gap_kind = "environment_incompatible"
        elif preferred_record is not None and not preferred_record.available:
            gap_kind = "unavailable"
        return StrategyGuidance(
            route="affordance_gap",
            rationale=f"Preferred route {missing!r} is not currently available; inspect or build a bounded replacement.",
            candidates=(),
            missing_affordance=missing,
            gap_kind=gap_kind,
            route_kind=route_kind_for(route),
            affordances=affordances,
            decision=GAP,
            completion_mode=OBSERVABLE_WORK_REQUIRED,
            discovery_state=str(discovery_state or "resolved"),
            turn_intent=intent.kind,
        )
    return StrategyGuidance(
        route,
        rationale,
        candidates,
        route_kind=route_kind_for(route),
        affordances=affordances,
        decision=ACT if actionable else RESPOND,
        completion_mode=OBSERVABLE_WORK_REQUIRED if actionable else RESPONSE_ONLY,
        discovery_state=str(discovery_state or "resolved"),
        turn_intent=intent.kind,
    )


def objective_requires_observable_work(objective: str) -> bool:
    """Compatibility wrapper around the frame-aware intent classifier."""
    return resolve_turn_intent(objective).requires_observable_work


def _coerce_affordance(value: str | Mapping[str, Any] | Any) -> StrategyAffordance:
    if isinstance(value, StrategyAffordance):
        return value
    if isinstance(value, str):
        return StrategyAffordance(id=value)
    if isinstance(value, Mapping):
        optimizer = value.get("optimizer")
        optimizer = optimizer if isinstance(optimizer, Mapping) else {}
        return StrategyAffordance(
            id=str(value.get("id") or ""),
            description=str(value.get("description") or ""),
            available=str(value.get("availability") or "available") == "available"
            and bool(value.get("available", True)),
            scope=str(value.get("scope") or "system"),
            dependency_ready=bool(
                value.get("dependency_ready", optimizer.get("dependency_available", True))
            ),
            environment_compatible=bool(
                value.get(
                    "environment_compatible",
                    optimizer.get("environment_compatible", True),
                )
            ),
            proof=dict(value.get("proof") or optimizer),
            effects=tuple(sorted(_string_values(value.get("effects")))),
            tags=tuple(sorted(_string_values(value.get("tags")))),
            output_schema=dict(value.get("output_schema") or {}),
        )
    identifier = str(getattr(value, "id", ""))
    availability = getattr(getattr(value, "availability", None), "value", "available")
    origin = getattr(getattr(value, "origin", None), "value", "system")
    return StrategyAffordance(
        id=identifier,
        description=str(getattr(value, "description", "") or ""),
        available=availability == "available",
        scope=origin,
        effects=tuple(sorted(_string_values(getattr(value, "effects", ())))),
        tags=tuple(sorted(_string_values(getattr(value, "tags", ())))),
        output_schema=dict(getattr(value, "output_schema", None) or {}),
    )


def _string_values(values: Any) -> set[str]:
    if isinstance(values, str):
        values = (values,)
    return {
        str(getattr(value, "value", value)).casefold()
        for value in (values or ())
        if str(getattr(value, "value", value)).strip()
    }


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", value.casefold()))


def _matches(identifier: str, preferred: str) -> bool:
    return identifier == preferred or identifier.startswith(preferred + ".")


def route_kind_for(route: str) -> str:
    return {
        "direct": "existing_primitive",
        "compose": "workflow_composition",
        "synthesize": "generated_capability",
        "evidence_acquisition": "research_evidence",
        "fusion": "fusion_shadow",
    }.get(route, "affordance_gap")


__all__ = [
    "ACT",
    "DISCOVER",
    "GAP",
    "OBSERVABLE_WORK_REQUIRED",
    "RESPOND",
    "RESPONSE",
    "OBSERVATION",
    "EXECUTION",
    "MUTATION",
    "EXTERNAL_ACTION",
    "CONTINUATION",
    "DEFINITELY_RESPONSE_ONLY",
    "MAY_REQUIRE_OBSERVATION_OR_ACTION",
    "RESPONSE_ONLY",
    "TurnIntent",
    "StrategyAffordance",
    "StrategyGuidance",
    "objective_requires_observable_work",
    "resolve_turn_intent",
    "is_explicit_response_turn",
    "select_strategy",
]
