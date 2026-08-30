from __future__ import annotations

import pytest

from athena.protocol.ids import new_id
from athena.state.database import Database
from athena.state.self_host import SelfHostMissionStore
from athena.state.tasks import TaskStore
from athena.self_host.risk import SelfHostRiskClassifier
from athena.self_host.reviewer import SelfHostIndependentReviewer


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database._ensure_ready()
    yield database
    await database.close()


async def test_self_host_mission_survives_store_reload(db):
    task_id = new_id("task")
    await TaskStore(db).insert_task(task_id, None, None, "bounded repair")
    first = SelfHostMissionStore(db)
    created = await first.create(
        project_root="/repo",
        objective="bounded repair",
        task_id=task_id,
        base_revision="abc123",
        design_bundle_hash="design-hash",
        gate_bundle_hash="gate-hash",
    )

    second = SelfHostMissionStore(db)
    loaded = await second.get(str(created["id"]))
    assert loaded is not None
    assert loaded["current_task_id"] == task_id
    assert loaded["plan"]["bounded"] is True


def test_self_host_risk_is_deterministic_and_operator_bound():
    resources = [
        {"path": "src/athena/kernel/kernel.py", "operation": "write"},
        {"path": "tests/unit/test_kernel.py", "operation": "write"},
    ]
    result = SelfHostRiskClassifier.classify(resources)
    assert result["level"] == "high"
    assert result["requires_operator_promotion"] is True
    assert result["requires_independent_review"] is True


def test_independent_review_requires_bound_certificate_and_all_checks():
    result = SelfHostIndependentReviewer.review(
        status="VERIFIED",
        certificate={
            "certificate_hash": "cert",
            "base_fingerprint": "base",
            "candidate_fingerprint": "candidate",
            "proof_authority": {
                "source_revision": "abc",
                "gate_bundle_hash": "gates",
            },
        },
        verification=[{"id": "gate", "passed": True}],
    )
    assert result["eligible"] is True
    assert result["independent"] is True
    assert result["evidence_hash"]
