from __future__ import annotations

import json

import pytest

from athena.capabilities.truth import TruthCapability
from athena.protocol.capabilities import CapabilityRequest, CapabilityResultStatus


class _WorldState:
    async def snapshot(self, **kwargs):
        return {
            "task_id": "task-truth",
            "claims": [
                {
                    "id": "claim-1",
                    "text": "tests pass",
                    "status": "VERIFIED",
                    "evidence": {
                        "execution_id": "exec-1",
                        "artifact_uri": "artifact://sha256/report",
                    },
                }
            ],
            "invariants": [],
            "invariant_results": [],
        }


class _Fabric:
    def created_this_task(self, task_id):
        return [
            {
                "id": "generated-1",
                "lifecycle_state": "STALE",
                "code_hash": "code-1",
                "schema_hash": "schema-1",
                "evidence_dependencies": [{"source_id": "source-1"}],
                "proof_record": {"all_passed": True},
            }
        ]


class _Service:
    _research_store = None
    _fabric = _Fabric()

    def world_state(self, task_id):
        return _WorldState()


@pytest.mark.asyncio
async def test_truth_status_unifies_claim_and_generated_proof_staleness():
    capability = TruthCapability(_Service())
    result = await capability.invoke(
        CapabilityRequest(
            capability_id="truth",
            task_id="task-truth",
            call_id="truth-1",
            arguments={"operation": "status"},
        )
    )

    assert result.status is CapabilityResultStatus.OK
    payload = json.loads(result.output)
    assert payload["status"] == "STALE"
    assert payload["claims"][0]["proof_refs"] == [
        {"kind": "execution", "subject": "exec-1"},
        {"kind": "artifact", "subject": "artifact://sha256/report"},
    ]
    assert payload["stale_capabilities"][0]["id"] == "generated-1"


@pytest.mark.asyncio
async def test_truth_explain_returns_one_claim_proof():
    capability = TruthCapability(_Service())
    result = await capability.invoke(
        CapabilityRequest(
            capability_id="truth",
            task_id="task-truth",
            call_id="truth-2",
            arguments={"operation": "explain", "claim_id": "claim-1"},
        )
    )

    assert result.status is CapabilityResultStatus.OK
    payload = json.loads(result.output)
    assert payload["id"] == "claim-1"
    assert payload["status"] == "VERIFIED"
    assert payload["proof_refs"][0]["subject"] == "exec-1"


@pytest.mark.asyncio
async def test_truth_dependencies_returns_claim_proof_inputs():
    capability = TruthCapability(_Service())
    result = await capability.invoke(
        CapabilityRequest(
            capability_id="truth",
            task_id="task-truth",
            call_id="truth-3",
            arguments={"operation": "dependencies", "claim_id": "claim-1"},
        )
    )

    assert result.status is CapabilityResultStatus.OK
    payload = json.loads(result.output)
    assert payload["claim_id"] == "claim-1"
    assert [item["subject"] for item in payload["dependencies"]] == [
        "exec-1",
        "artifact://sha256/report",
    ]
