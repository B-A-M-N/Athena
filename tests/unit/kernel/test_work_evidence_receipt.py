"""Regression test for P1: WorkEvidence from canonical dispatcher receipts.

Verifies that when the dispatcher stamps resolved_effects onto result
metadata, the evidence classifier prefers them over name heuristics.
"""

from __future__ import annotations

from athena.kernel.termination import result_qualifies_as_work_evidence
from athena.strategy import EXECUTION, MUTATION, OBSERVATION, EXTERNAL_ACTION


class MockResult:
    def __init__(self, capability_id, ok=True, metadata=None, ref_uri=None):
        self.capability_id = capability_id
        self.ok = ok
        self.metadata = metadata or {}
        self.ref_uri = ref_uri
        self.call_id = "call-1"


def test_resolved_effects_override_name_heuristics():
    """A generated capability with a misleading name but canonical
    resolved_effects should be classified by the effects."""
    # A generated "analyst" tool that writes files — the name says
    # "analyst" (sounds like a reader) but the effects say write_local.
    result = MockResult(
        capability_id="gen.analyst",
        metadata={"resolved_effects": ["write_local"]},
    )
    evidence = result_qualifies_as_work_evidence(result)
    assert evidence is not None
    assert evidence.kind == MUTATION


def test_resolved_effects_execute():
    result = MockResult(
        capability_id="execute",
        metadata={"resolved_effects": ["execute", "spawn_process"]},
    )
    evidence = result_qualifies_as_work_evidence(result)
    assert evidence is not None
    assert evidence.kind == EXECUTION


def test_resolved_effects_read():
    result = MockResult(
        capability_id="fs",
        metadata={"resolved_effects": ["read_local"]},
    )
    evidence = result_qualifies_as_work_evidence(result)
    assert evidence is not None
    assert evidence.kind == OBSERVATION


def test_resolved_effects_external():
    result = MockResult(
        capability_id="research",
        metadata={"resolved_effects": ["network_write"]},
    )
    evidence = result_qualifies_as_work_evidence(result)
    assert evidence is not None
    assert evidence.kind == EXTERNAL_ACTION


def test_fallback_to_name_heuristics_when_no_receipt():
    """Without resolved_effects, fall back to name-based classification."""
    result = MockResult(
        capability_id="execute",
        metadata={},
    )
    evidence = result_qualifies_as_work_evidence(result)
    assert evidence is not None
    assert evidence.kind == EXECUTION


def test_receipt_with_mutation_and_artifact():
    """Mutation takes precedence over artifact when both are present."""
    result = MockResult(
        capability_id="fs",
        metadata={"resolved_effects": ["write_local"]},
        ref_uri="artifact://x",
    )
    evidence = result_qualifies_as_work_evidence(result)
    assert evidence is not None
    assert evidence.kind == MUTATION


def test_receipt_mispredicts_operation_name():
    """A capability whose operation name says 'inspect' but whose
    resolved effects prove it actually executed code — the receipt wins."""
    result = MockResult(
        capability_id="diagnostic.tool",
        metadata={
            "resolved_effects": ["execute", "spawn_process", "read_local"],
            "operation": "inspect",
        },
    )
    evidence = result_qualifies_as_work_evidence(result)
    assert evidence is not None
    # EXECUTE takes precedence over READ in the resolved_effects set
    assert evidence.kind == EXECUTION
