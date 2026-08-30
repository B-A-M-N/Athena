"""Unit tests for TerminationEvaluator / TerminationDecision (BHV-005)."""

from __future__ import annotations


import pytest

from athena.kernel.termination import TerminationDecision, TerminationEvaluator
from athena.protocol.models import ModelResponse, UsageInfo
from athena.protocol.messages import CapabilityCallBlock, TextBlock
from athena.protocol.tasks import Criterion, MutationMode, TaskSpec, TaskStatus, WorkspaceSpec


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
