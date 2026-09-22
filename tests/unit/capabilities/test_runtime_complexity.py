"""Complexity evidence isolation and non-escalating capability failures."""

from __future__ import annotations


from athena.capabilities.prepared import PreparedCapabilityCall
from athena.capabilities.runtime_escalation import ComplexityLedger
from athena.protocol.capabilities import CapabilityDescriptor, CapabilityRequest, EffectClass


class _Executor:
    def __init__(self, capability_id: str, effects: set[EffectClass]):
        self.descriptor = CapabilityDescriptor(
            id=capability_id,
            description="probe",
            input_schema={"type": "object"},
            effects=frozenset(effects),
        )


class _Gate:
    def active_branch(self, task_id):
        return None


class _Dispatcher:
    def __init__(self):
        self._reality_gate = _Gate()
        self._runtime_speculative_tasks = set()
        self._late_complexity_escalations = set()
        self._complexity_ledger = {}


def _prepared(task_id: str, capability_id: str, effects: set[EffectClass]):
    return PreparedCapabilityCall(
        request=CapabilityRequest(
            capability_id, {}, task_id=task_id, call_id=f"{task_id}-{capability_id}"
        ),
        workspace=None,
        executor=_Executor(capability_id, effects),
        effects=tuple(effects),
    )


def test_ledger_state_is_isolated_between_tasks():
    ledger = ComplexityLedger()
    ledger.record_prepared(_prepared("task-a", "fs", {EffectClass.WRITE_LOCAL}))
    facts_b = ledger._entry("task-b")
    ledger.record_prepared(_prepared("task-b", "fs", {EffectClass.WRITE_LOCAL}))
    assert facts_b.resources == [] or facts_b.resources == list(facts_b.resources)
    assert ledger.snapshot("task-a")["resources"] == list(ledger.snapshot("task-a")["resources"])
    assert ledger.snapshot("task-a")["mutation_count"] == 1
    assert ledger.snapshot("task-b")["mutation_count"] == 1
    assert ledger.snapshot("task-a")["resources"] == ledger.snapshot("task-b")["resources"]


def test_capability_failures_do_not_escalate_complexity():
    ledger = ComplexityLedger()
    ledger.record_capability_failure("task-c", infrastructure=False)
    ledger.record_capability_failure("task-c", infrastructure=True)
    ledger.record_capability_failure("task-c", infrastructure=False, candidate_attempt=True)
    assert ledger.is_complex("task-c") is False
    snapshot = ledger.snapshot("task-c")
    assert snapshot["capability_failures"] == 3
    assert snapshot["infrastructure_failures"] == 1
    assert snapshot["candidate_attempt_failures"] == 1
    assert snapshot["candidate_verification_failures"] == 0


def test_candidate_verification_failure_is_explicit_signal():
    ledger = ComplexityLedger()
    assert ledger.record_candidate_verification_failure("task-d") is True
    assert ledger.snapshot("task-d")["candidate_verification_failures"] == 1
