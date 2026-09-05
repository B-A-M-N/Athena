"""Natural-language agent behavior corpus.

The release lane that keeps Athena behaving like an agent instead of
oscillating between over-eager and over-restricted. Each row is an ordinary
user utterance; each assertion covers the whole pre-model pipeline:

* the narrow response channel decision (``is_explicit_response_turn``);
* the compiled capability surface the model will actually see;
* whether the provider must be tool-capable (``needs_tools``);
* compiled skill/memory retrieval;
* the strategy's completion mode (what counts as done).

A row that regresses here breaks a real user turn, not an internal contract.
"""

from __future__ import annotations

import pytest

from athena.context.compiler import ContextCompiler
from athena.protocol.capabilities import CapabilityDescriptor
from athena.protocol.tasks import ModelPolicy, TaskSpec
from athena.strategy import is_explicit_response_turn

# ---------------------------------------------------------------------- #
# The corpus
# ---------------------------------------------------------------------- #

# Turns that are DEFINITELY self-contained conversation: no observable
# state, no continuation. Tools may be suppressed.
DEFINITE_RESPONSE = [
    "hello",
    "hi",
    "thanks",
    "how are you",
    "tell me a joke",
    "tell me a short joke",
    "explain recursion in plain language",
    "write a poem about winter",
    "how do I create a Python file?",
    "what is the capital of France?",
    "who wrote Hamlet?",
    "what happens if I run pytest without arguments?",
]

# Turns that REFERENCE OBSERVABLE STATE. Grammatically questions, but an
# honest answer requires observing the workspace. Never response-only.
WORKSPACE_QUESTIONS = [
    "What changed in this repo?",
    "What does README.md say?",
    "Why is this test failing?",
    "How many files are in this folder?",
    "Could you take a look at the code?",
    "Take a look at the code",
    "Compare README.md and SPEC.md",
    "Explain why pytest is failing based on the repo",
    "Can you help me debug this repository?",
    "Look at the logs and tell me why it crashed",
    "what does this function do?",
    "check my config",
]

# CONTINUATION language: only meaningful against prior context or work.
# Never response-only.
CONTINUATIONS = [
    "fix it",
    "run it",
    "test it",
    "go ahead",
    "yes, do that",
    "please do it",
    "try again",
    "continue",
    "keep going",
    "use the first one",
    "same thing here",
    "then commit it",
]

# MEMORY-REFERENTIAL turns: they need durable memory retrieval even when
# their surface grammar is a question or a preference.
MEMORY_REFERENTIAL = [
    "use the same setup as before",
    "do it the way we decided",
    "what did we use last time?",
    "use my usual configuration",
    "apply my preference from last time",
]

# Explicit action work (sanity anchors for the actionable side).
ACTION_WORK = [
    "PROSE_READ the project file",
    "open the configuration",
    "do the thing",
    "fix the failing test in module X",
    "commit these changes",
]


# ---------------------------------------------------------------------- #
# Channel decisions
# ---------------------------------------------------------------------- #


def test_definite_conversation_is_response_only():
    for utterance in DEFINITE_RESPONSE:
        assert is_explicit_response_turn(utterance), utterance


def test_workspace_questions_are_tool_eligible():
    for utterance in WORKSPACE_QUESTIONS:
        assert not is_explicit_response_turn(utterance), utterance


def test_continuations_are_tool_eligible():
    for utterance in CONTINUATIONS:
        assert not is_explicit_response_turn(utterance), utterance


def test_memory_referential_turns_are_tool_eligible():
    for utterance in MEMORY_REFERENTIAL:
        assert not is_explicit_response_turn(utterance), utterance


def test_action_work_is_tool_eligible():
    for utterance in ACTION_WORK:
        assert not is_explicit_response_turn(utterance), utterance


# ---------------------------------------------------------------------- #
# Whole-pipeline rows: classifier + compiled context + strategy
# ---------------------------------------------------------------------- #

# The registry doubles as a minimal workspace: every capability the corpus
# turns might legitimately reach, so assertions can distinguish "the model
# can see a way to work" from "the model was starved".
_CORPUS_REGISTRY = (
    CapabilityDescriptor(id="fs", description="read and write files", input_schema={}),
    CapabilityDescriptor(
        id="git", description="inspect and change repository state", input_schema={}
    ),
    CapabilityDescriptor(id="execute", description="run a bounded command", input_schema={}),
    CapabilityDescriptor(
        id="diagnostics", description="inspect failures and health", input_schema={}
    ),
    CapabilityDescriptor(id="memory", description="recall durable memory", input_schema={}),
    CapabilityDescriptor(
        id="capabilities", description="search the capability fabric", input_schema={}
    ),
)


class _CorpusRegistry:
    def __init__(self, descriptors):
        self._descriptors = list(descriptors)

    async def list_descriptors(self):
        return list(self._descriptors)

    async def search(self, query, **kwargs):
        del kwargs  # lexical miss on purpose: fallback bundles are the contract
        return []


def _task(objective: str) -> TaskSpec:
    return TaskSpec(id="corpus-task", objective=objective)


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", DEFINITE_RESPONSE, ids=lambda u: f"response::{u[:36]}")
async def test_response_turns_compile_tool_less(utterance):
    context = await ContextCompiler(capability_registry=_CorpusRegistry(_CORPUS_REGISTRY)).compile(
        _task(utterance)
    )

    assert context.capability_definitions == (), utterance
    assert context.requirements.needs_tools is False, utterance
    assert context.strategy.completion_mode == "response_only", utterance
    assert context.strategy.decision == "respond", utterance


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", WORKSPACE_QUESTIONS, ids=lambda u: f"workspace::{u[:36]}")
async def test_workspace_questions_get_a_working_tool_surface(utterance):
    context = await ContextCompiler(capability_registry=_CorpusRegistry(_CORPUS_REGISTRY)).compile(
        _task(utterance)
    )

    # The model must be able to actually look: at least one workspace
    # observation primitive plus reflection are visible, and the provider
    # must be tool-capable.
    visible = {descriptor.id for descriptor in context.capability_definitions}
    assert visible, utterance
    assert visible & {"fs", "git", "execute", "diagnostics"}, utterance
    assert "capabilities" in visible, utterance
    assert context.requirements.needs_tools is True, utterance
    # Tool availability alone is not enough (P0-6): answering a question
    # about the workspace honestly REQUIRES observed evidence, so the
    # completion mode must refuse a hallucinated prose-only finish.
    assert context.strategy.completion_mode == "observable_work_required", utterance


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", CONTINUATIONS, ids=lambda u: f"continuation::{u[:36]}")
async def test_continuations_are_tool_eligible_end_to_end(utterance):
    context = await ContextCompiler(capability_registry=_CorpusRegistry(_CORPUS_REGISTRY)).compile(
        _task(utterance)
    )

    assert context.requirements.needs_tools is True, utterance
    assert context.strategy.completion_mode == "observable_work_required", utterance


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", MEMORY_REFERENTIAL, ids=lambda u: f"memory::{u[:36]}")
async def test_memory_referential_turns_retrieve_memory(utterance):
    context = await ContextCompiler(capability_registry=_CorpusRegistry(_CORPUS_REGISTRY)).compile(
        _task(utterance)
    )

    assert context.requirements.needs_tools is True, utterance
    assert "capabilities" in {descriptor.id for descriptor in context.capability_definitions}, (
        utterance
    )
    # Memory-referential questions ("what did we use last time?") ask what
    # actually happened; an honest answer requires retrieval, so completion
    # must refuse a prose-only recall hallucination.
    assert context.strategy.completion_mode == "observable_work_required", utterance


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", ACTION_WORK, ids=lambda u: f"action::{u[:36]}")
async def test_action_turns_require_observable_work(utterance):
    context = await ContextCompiler(capability_registry=_CorpusRegistry(_CORPUS_REGISTRY)).compile(
        _task(utterance)
    )

    assert context.requirements.needs_tools is True, utterance
    assert context.strategy.completion_mode == "observable_work_required", utterance
    assert context.strategy.decision in {"act", "discover"}, utterance


@pytest.mark.asyncio
async def test_tool_required_policy_never_compiles_empty_surface():
    for utterance in WORKSPACE_QUESTIONS + CONTINUATIONS + ACTION_WORK:
        task = TaskSpec(
            id="corpus-task",
            objective=utterance,
            model_policy=ModelPolicy(require_tools=True),
        )
        context = await ContextCompiler(
            capability_registry=_CorpusRegistry(_CORPUS_REGISTRY)
        ).compile(task)
        assert context.capability_definitions, utterance
