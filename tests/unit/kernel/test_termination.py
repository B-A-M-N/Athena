"""Unit tests for TerminationEvaluator / TerminationDecision (BHV-005)."""

from __future__ import annotations


import pytest

from athena.kernel.termination import TerminationDecision, TerminationEvaluator, WorkEvidence
from athena.protocol.models import ModelResponse, UsageInfo
from athena.protocol.messages import CapabilityCallBlock, TextBlock
from athena.protocol.tasks import Criterion, MutationMode, TaskSpec, TaskStatus, WorkspaceSpec
from athena.strategy import OBSERVABLE_WORK_REQUIRED


@pytest.fixture
def evaluator():
    return TerminationEvaluator(default_max_iterations=100)


def _response(blocks=(), *, finish_reason="stop"):
    return ModelResponse(
        request_id="r1",
        model="fake-1",
        provider="fake",
        blocks=tuple(blocks),
        finish_reason=finish_reason,
        usage=UsageInfo(),
    )


async def test_decision_constructs_terminal_and_nonterminal():
    term = TerminationDecision(terminal=True, reason="done", status="COMPLETE")
    assert term.terminal is True
    assert term.reason == "done"

    non = TerminationDecision(terminal=False, reason="more")
    assert non.terminal is False


@pytest.mark.athena_claim("BHV-005")
@pytest.mark.athena_evidence("test", "invariant")
async def test_final_text_with_no_criteria_is_terminal(evaluator):
    task = TaskSpec(id="t1", objective="hello")
    block = TextBlock(type="text", text="hi")
    decision = await evaluator.evaluate(task, _response([block]), iterations=1)
    assert decision.terminal is True
    assert decision.status is not None and decision.status.value == "COMPLETE"
    assert decision.reason == "objective satisfied"


@pytest.mark.athena_claim("BHV-005")
@pytest.mark.athena_evidence("test", "invariant")
async def test_capability_calls_are_not_terminal(evaluator):
    task = TaskSpec(id="t2", objective="do work")
    call = CapabilityCallBlock(capability_id="tools.execute", arguments={})
    decision = await evaluator.evaluate(task, _response([call]), iterations=1)
    assert decision.terminal is False
    assert decision.reason == "capability_calls_present"


@pytest.mark.athena_claim("BHV-005", "BHV-006")
@pytest.mark.athena_evidence("test", "invariant")
async def test_truth_outranks_success_unverified_criteria(evaluator):
    """Claimed completion with unverified required criteria must be PARTIAL,
    never COMPLETE (BHV-005 / BHV-006)."""
    task = TaskSpec(
        id="t3",
        objective="make report",
        acceptance_criteria=(Criterion(id="c1", description="report exists"),),
    )
    block = TextBlock(type="text", text="I am done")
    decision = await evaluator.evaluate(
        task, _response([block], finish_reason="end_turn"), iterations=2
    )
    assert decision.terminal is True
    assert decision.status.value == "PARTIAL"
    assert "c1" in decision.unresolved


async def test_required_research_evidence_needs_a_ready_bundle_receipt():
    class _PassingVerifier:
        async def verify(self, task, criteria):
            return [True for _ in criteria]

    task = TaskSpec(
        id="research-proof",
        objective="research the release",
        acceptance_criteria=(
            Criterion(
                id="research-bundle",
                description="research bundle is ready",
                evidence_required=True,
            ),
        ),
    )
    evaluator = TerminationEvaluator(acceptance_verifier=_PassingVerifier())
    decision = await evaluator.evaluate(
        task,
        _response([TextBlock(type="text", text="done")]),
        iterations=1,
        work_evidence=(
            WorkEvidence(kind="observation", capability_id="research", research_ready=False),
        ),
    )
    assert decision.status is TaskStatus.PARTIAL
    assert decision.unresolved == ("research-bundle",)

    ready = await evaluator.evaluate(
        task,
        _response([TextBlock(type="text", text="done")]),
        iterations=1,
        work_evidence=(
            WorkEvidence(kind="observation", capability_id="research", research_ready=True),
        ),
    )
    assert ready.status is TaskStatus.COMPLETE


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ([], ("first", "second")),
        ([True], ("first", "second")),
        ([True, True, True], ("first", "second")),
        ([True, "true"], ("second",)),
    ],
)
async def test_verifier_results_must_match_required_criteria_exactly(results, expected):
    class Verifier:
        async def verify(self, task, criteria):
            return results

    task = TaskSpec(
        id="criteria-cardinality",
        objective="verify the result",
        acceptance_criteria=(
            Criterion(id="first", description="first criterion"),
            Criterion(id="second", description="second criterion"),
        ),
    )
    evaluator = TerminationEvaluator(acceptance_verifier=Verifier())

    decision = await evaluator.evaluate(
        task,
        _response([TextBlock(type="text", text="done")]),
        iterations=1,
    )

    assert decision.status is TaskStatus.PARTIAL
    assert decision.unresolved == expected


async def test_verifier_exception_leaves_every_required_criterion_unresolved():
    class Verifier:
        async def verify(self, task, criteria):
            raise RuntimeError("verifier unavailable")

    task = TaskSpec(
        id="criteria-exception",
        objective="verify the result",
        acceptance_criteria=(
            Criterion(id="first", description="first criterion"),
            Criterion(id="second", description="second criterion"),
        ),
    )
    evaluator = TerminationEvaluator(acceptance_verifier=Verifier())

    decision = await evaluator.evaluate(
        task,
        _response([TextBlock(type="text", text="done")]),
        iterations=1,
    )

    assert decision.status is TaskStatus.PARTIAL
    assert decision.unresolved == ("first", "second")


async def test_reality_coordinator_owns_speculative_candidate_proof():
    class _CountingVerifier:
        calls = 0

        async def verify(self, task, criteria):
            self.calls += 1
            return [True for _ in criteria]

    verifier = _CountingVerifier()
    evaluator = TerminationEvaluator(
        acceptance_verifier=verifier,
        defer_reality_verification=lambda task: task.id == "candidate",
    )
    task = TaskSpec(
        id="candidate",
        objective="patch source",
        acceptance_criteria=(Criterion(id="command", description="proof"),),
        workspace=WorkspaceSpec(
            id="workspace",
            root="/tmp/project",
            mutation_mode=MutationMode.SPECULATIVE,
        ),
    )

    decision = await evaluator.evaluate(
        task, _response([TextBlock(type="text", text="done")]), iterations=1
    )

    assert decision.status is TaskStatus.COMPLETE
    assert "delegated" in decision.reason
    assert verifier.calls == 0


async def test_action_task_without_observable_evidence_is_partial():
    task = TaskSpec(id="action-no-proof", objective="read the project file")

    decision = await TerminationEvaluator().evaluate(
        task,
        _response([TextBlock(type="text", text="I read it and it looks good")]),
        iterations=1,
        completion_mode=OBSERVABLE_WORK_REQUIRED,
        observed_work=False,
    )

    assert decision.terminal is True
    assert decision.status is TaskStatus.PARTIAL
    assert decision.unresolved == ("observable_work",)


async def test_action_task_with_observable_evidence_can_complete():
    task = TaskSpec(id="action-proof", objective="read the project file")

    decision = await TerminationEvaluator().evaluate(
        task,
        _response([TextBlock(type="text", text="The file contains the requested value")]),
        iterations=1,
        completion_mode=OBSERVABLE_WORK_REQUIRED,
        observed_work=True,
    )

    assert decision.terminal is True
    assert decision.status is TaskStatus.COMPLETE


# ---------------------------------------------------------------------- #
# Verifier evidence reuse (task #13): ONE test/verification run counts once
# and proves twice — a verified required criterion is itself the observable
# work proof, so OBSERVABLE_WORK_REQUIRED is satisfied WITHOUT a separate
# model execution (termination.verified_criteria_evidence reuse).
# ---------------------------------------------------------------------- #


class _CountingVerifier2:
    """Records every verify() invocation and its view of the criteria."""

    def __init__(self, outcome=True):
        self.calls = 0
        self.outcome = outcome

    async def verify(self, task, criteria):
        self.calls += 1
        return [self.outcome for _ in criteria]


async def test_verified_criteria_count_works_and_proves_twice():
    """For state-shaped objectives (observation/response), a verified criterion
    alone satisfies both the acceptance gate and the observable-work gate.
    For action-shaped objectives (execution/mutation/external), verified
    criteria alone are insufficient — causal work evidence is required."""
    verifier = _CountingVerifier2(outcome=True)
    evaluator = TerminationEvaluator(acceptance_verifier=verifier)

    # State-shaped objective: "what does the README say?" — verified criteria ARE the work
    task = TaskSpec(
        id="criteria-evidence",
        objective="what does the README say?",
        acceptance_criteria=(Criterion(id="c-readme", description="README was read"),),
    )

    decision = await evaluator.evaluate(
        task,
        _response([TextBlock(type="text", text="the README says...")]),
        iterations=1,
        completion_mode=OBSERVABLE_WORK_REQUIRED,
        observed_work=False,
        work_evidence=(),
    )

    # COMPLETE: the verified criterion is sufficient proof for a state-shaped objective
    assert decision.terminal is True
    assert decision.status is TaskStatus.COMPLETE
    assert decision.unresolved == ()
    assert verifier.calls == 1

    # Action-shaped objective: "fix the failing test" — verified criteria alone
    # are NOT sufficient; need causal work evidence
    verifier2 = _CountingVerifier2(outcome=True)
    evaluator2 = TerminationEvaluator(acceptance_verifier=verifier2)
    action_task = TaskSpec(
        id="criteria-action",
        objective="fix the failing test",
        acceptance_criteria=(Criterion(id="c-tests", description="tests pass"),),
    )
    decision2 = await evaluator2.evaluate(
        action_task,
        _response([TextBlock(type="text", text="all tests green")]),
        iterations=1,
        completion_mode=OBSERVABLE_WORK_REQUIRED,
        observed_work=False,
        work_evidence=(),
    )
    # PARTIAL: action-shaped objective requires causal work evidence
    assert decision2.terminal is True
    assert decision2.status is TaskStatus.PARTIAL
