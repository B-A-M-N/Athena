"""Unit tests for TerminationEvaluator / TerminationDecision (BHV-005)."""

from __future__ import annotations


import pytest

from athena.kernel.termination import TerminationDecision, TerminationEvaluator
from athena.protocol.models import ModelResponse, UsageInfo
from athena.protocol.messages import CapabilityCallBlock, TextBlock
from athena.protocol.tasks import Criterion, TaskSpec


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


async def test_final_text_with_no_criteria_is_terminal(evaluator):
    task = TaskSpec(id="t1", objective="hello")
    block = TextBlock(type="text", text="hi")
    decision = await evaluator.evaluate(
        task, _response([block]), iterations=1
    )
    assert decision.terminal is True
    assert decision.status is not None and decision.status.value == "COMPLETE"
    assert decision.reason == "objective satisfied"


async def test_capability_calls_are_not_terminal(evaluator):
    task = TaskSpec(id="t2", objective="do work")
    call = CapabilityCallBlock(capability_id="tools.execute", arguments={})
    decision = await evaluator.evaluate(task, _response([call]), iterations=1)
    assert decision.terminal is False
    assert decision.reason == "capability_calls_present"


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