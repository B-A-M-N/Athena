"""Admission-level speculative coding authority."""

from __future__ import annotations

from athena.protocol.tasks import AgentRequest, AutonomyLevel, MutationMode
from athena.service.service import AthenaService
from athena.service.work_classification import (
    SpeculationDepth,
    WorkClass,
    decide_speculation,
)


def _spec(prompt: str):
    return AthenaService.in_memory()._build_task_spec(
        AgentRequest(prompt=prompt),
        "session-classification",
    )


def test_complex_coding_prompt_defaults_to_speculative_at_admission():
    for prompt in (
        "fix the parser across the repo and run tests",
        "refactor the authentication subsystem and update all callers",
        "migrate the module boundary and verify the suite",
    ):
        spec = _spec(prompt)
        assert spec.workspace is not None
        assert spec.workspace.mutation_mode is MutationMode.SPECULATIVE, prompt
        assert spec.metadata["autonomy"] == AutonomyLevel.SUPERVISED.value
        assert spec.metadata["_athena_work_class"] == WorkClass.COMPLEX_CODING.value
        assert spec.metadata["_athena_speculation_depth"] == SpeculationDepth.SINGLE_CANDIDATE.value


def test_explicit_direct_mode_stays_the_operator_escape_hatch():
    spec = AthenaService.in_memory()._build_task_spec(
        AgentRequest(
            prompt="refactor the authentication subsystem",
            mutation_mode=MutationMode.DIRECT,
        ),
        "session-direct",
    )
    assert spec.workspace is not None
    assert spec.workspace.mutation_mode is MutationMode.DIRECT
    # The work class remains visible even when an operator explicitly opts out.
    assert spec.metadata["_athena_work_class"] == WorkClass.COMPLEX_CODING.value


def test_simple_and_noncoding_work_do_not_claim_complex_speculation():
    assert WorkClass.of("fix the typo") is WorkClass.SIMPLE_EDIT
    assert WorkClass.of("tell me a joke") is WorkClass.NON_CODING
    assert decide_speculation("fix the typo").depth is SpeculationDepth.NONE
    assert decide_speculation("tell me a joke").work_class is WorkClass.NON_CODING
    simple = _spec("fix the typo")
    assert simple.workspace is not None
    assert simple.workspace.mutation_mode is MutationMode.DIRECT
    assert simple.metadata["_athena_work_class"] == WorkClass.SIMPLE_EDIT.value
    assert simple.metadata["_athena_speculation_depth"] == SpeculationDepth.NONE.value


def test_ambiguous_mutating_coding_defaults_to_complex():
    for prompt in (
        "implement OAuth login with refresh tokens and update tests",
        "add a new caching layer and integrate it with the API",
        "fix the parser and the serializer",
        "upgrade the database schema and update migrations",
        "implement the feature in src/auth.py",
        "rewrite the routing logic",
    ):
        assert WorkClass.of(prompt) is WorkClass.COMPLEX_CODING, prompt
        spec = _spec(prompt)
        assert spec.workspace is not None
        assert spec.workspace.mutation_mode is MutationMode.SPECULATIVE
        assert spec.metadata["_athena_work_class"] == "complex_coding"
        assert spec.metadata["_athena_speculation_depth"] == "single_candidate"


def test_multi_candidate_only_through_explicit_or_repeated_failure():
    """Multi-candidate is exceptional: bounded by explicit request or >=2
    verification failures, never inferred from prose complexity alone."""
    assert decide_speculation("refactor auth").depth is SpeculationDepth.SINGLE_CANDIDATE
    assert (
        decide_speculation("refactor auth", explicit_comparison=True).depth
        is SpeculationDepth.MULTI_CANDIDATE
    )
    assert (
        decide_speculation("refactor auth", verification_failures=2).depth
        is SpeculationDepth.MULTI_CANDIDATE
    )
    assert (
        decide_speculation("refactor auth", verification_failures=1).depth
        is SpeculationDepth.SINGLE_CANDIDATE
    )
    # Non-coding never escalates to multi-candidate.
    assert (
        decide_speculation("tell me a joke", explicit_comparison=True).depth
        is SpeculationDepth.NONE
    )


def test_broad_scope_dominates_simple_edit_wording():
    for prompt in (
        "fix the typo across the entire repository",
        "one-line change across the codebase",
        "fix a typo and run the full test suite",
        "small wording cleanup throughout the project",
    ):
        assert WorkClass.of(prompt) is WorkClass.COMPLEX_CODING, prompt
