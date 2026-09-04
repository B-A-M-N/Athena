from datetime import timedelta

import pytest

from athena.protocol.messages import utcnow
from athena.state.database import Database
from athena.workflows.models import WorkflowStep
from athena.workflows.store import WorkflowStore


def _steps(path: str = "README.md") -> tuple[WorkflowStep, ...]:
    return (
        WorkflowStep(
            id="step_1",
            capability_id="fs",
            arguments={"operation": "read", "path": path},
        ),
        WorkflowStep(
            id="step_2",
            capability_id="execute",
            arguments={"command": "pytest -q"},
        ),
    )


@pytest.mark.asyncio
async def test_pending_workflow_observation_survives_restart_and_requires_distinct_task(
    tmp_path,
):
    db_path = tmp_path / "athena.sqlite"
    first_db = Database(str(db_path))
    first_store = WorkflowStore(first_db)
    first = await first_store.save_pending_observation(
        "signature-1",
        task_id="task-1",
        steps=_steps(),
        workspace_id="repo",
        workspace_revision="rev-1",
        verification={"status": "COMPLETE", "evidence_count": 2},
        observed_at=utcnow().isoformat(),
    )
    assert first.lifecycle_state == "PENDING_OBSERVATION"
    await first_db.close()

    second_db = Database(str(db_path))
    second_store = WorkflowStore(second_db)
    second = await second_store.save_pending_observation(
        "signature-1",
        task_id="task-2",
        steps=_steps("src/main.py"),
        workspace_id="repo",
        workspace_revision="rev-2",
        verification={"status": "COMPLETE", "artifact_count": 1},
        observed_at=utcnow().isoformat(),
    )

    assert second.lifecycle_state == "CANDIDATE"
    assert second.provenance["successful_observations"] == 2
    assert second.provenance["observed_task_ids"] == ["task-1", "task-2"]
    assert len(second.provenance["observations"]) == 2
    assert second.provenance["observations"][0]["workspace_revision"] == "rev-1"
    assert second.provenance["observations"][1]["verification"]["artifact_count"] == 1

    assert await second_store.get(second.id, task_id="task-1") is not None
    assert await second_store.get(second.id, task_id="task-2") is None
    rows = await second_db.fetch_all(
        "SELECT argument_shape, workspace_revision FROM workflow_observations "
        "WHERE trace_signature = ? ORDER BY task_id",
        ("signature-1",),
    )
    assert len(rows) == 2
    assert rows[0]["workspace_revision"] == "rev-1"
    assert '"arguments"' in rows[0]["argument_shape"]
    await second_db.close()


@pytest.mark.asyncio
async def test_workflow_observation_retention_expires_and_compacts(tmp_path):
    db = Database(str(tmp_path / "athena.sqlite"))
    store = WorkflowStore(db)
    old = (utcnow() - timedelta(days=31)).isoformat()
    await store.save_pending_observation(
        "old-signature",
        task_id="old-task",
        steps=_steps(),
        observed_at=old,
    )
    # Saving a current observation applies the production retention sweep.
    await store.save_pending_observation(
        "new-signature",
        task_id="new-task",
        steps=_steps("new.md"),
        observed_at=utcnow().isoformat(),
    )
    old_rows = await db.fetch_all(
        "SELECT id FROM workflow_observations WHERE trace_signature = ?",
        ("old-signature",),
    )
    assert old_rows == []
    await db.close()
