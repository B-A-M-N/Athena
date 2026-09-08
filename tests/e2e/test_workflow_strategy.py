"""High-level strategy -> workflow -> execution -> replay acceptance."""

from __future__ import annotations

import json

import pytest

from athena.capabilities.dispatcher import SuspendedCall
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResultStatus,
)
from athena.service.service import AthenaService
from athena.strategy import select_strategy


@pytest.mark.dsh_release
@pytest.mark.athena_claim("ATHENA-WORKFLOW-001")
@pytest.mark.athena_evidence("e2e")
@pytest.mark.asyncio
async def test_strategy_composes_nested_workflow_and_replays_durably() -> None:
    """Exercise the model-visible workflow affordance through a real service."""
    service = AthenaService.in_memory()
    service.config.autonomy = "autonomous"
    await service.start()
    try:
        await service._store_tasks.insert_task(  # noqa: SLF001 - release seam
            "workflow-e2e-task",
            None,
            None,
            "compose a release workflow",
            autonomy="autonomous",
            workspace=service._default_workspace,  # noqa: SLF001
        )
        guidance = select_strategy(
            "compose a release workflow",
            ("workflow", "execute", "fs"),
        )
        assert guidance.route == "compose"
        assert "workflow" in guidance.candidates

        async def dispatch(arguments: dict, call_id: str):
            return await service._dispatcher.dispatch(  # noqa: SLF001 - release seam
                CapabilityRequest(
                    capability_id="workflow",
                    task_id="workflow-e2e-task",
                    call_id=call_id,
                    origin=CapabilityRequestOrigin.MODEL,
                    arguments=arguments,
                ),
                workspace=service._default_workspace,  # noqa: SLF001
                profile=service.config.autonomy_level,
            )

        async def dispatch_after_approval(arguments: dict, call_id: str):
            result = await dispatch(arguments, call_id)
            if isinstance(result, SuspendedCall):
                await service.approve(result.approval_id, granted=True, scope="task")
                result = await dispatch(arguments, call_id)
            return result

        child = await dispatch(
            {
                "operation": "create",
                "name": "workflow_child",
                "description": "bounded child computation",
                "steps": [
                    {
                        "id": "child_execute",
                        "capability": "execute",
                        "arguments": {"language": "shell", "code": "printf child"},
                    }
                ],
            },
            "workflow-create-child",
        )
        assert child.status is CapabilityResultStatus.OK, child.error
        child_id = child.output

        parent = await dispatch(
            {
                "operation": "create",
                "name": "workflow_parent",
                "description": "nested release composition",
                "steps": [
                    {"id": "nested", "workflow": child_id},
                    {
                        "id": "parent_execute",
                        "capability": "execute",
                        "depends_on": ["nested"],
                        "arguments": {"language": "shell", "code": "printf parent"},
                    },
                ],
            },
            "workflow-create-parent",
        )
        assert parent.status is CapabilityResultStatus.OK, parent.error
        parent_id = parent.output

        run_arguments = {"operation": "run", "workflow_id": parent_id}
        approval = await dispatch(run_arguments, "workflow-run-parent")
        assert isinstance(approval, SuspendedCall)
        await service.approve(approval.approval_id, granted=True, scope="task")
        first = await dispatch(run_arguments, "workflow-run-parent")
        assert first.status is CapabilityResultStatus.OK, first.error
        first_payload = json.loads(first.output)
        assert first_payload["status"] == "completed"
        output_text = json.dumps(first_payload["outputs"])
        assert "child" in output_text
        assert "parent" in output_text
        run_id = first_payload["run_id"]

        replay = await dispatch_after_approval(
            {"operation": "run", "workflow_id": parent_id, "run_id": run_id},
            "workflow-replay-parent",
        )
        assert replay.status is CapabilityResultStatus.OK, replay.error
        replay_payload = json.loads(replay.output)
        assert replay_payload["status"] == "completed"
        assert replay_payload["run_id"] == run_id

        failed_child = await dispatch(
            {
                "operation": "create",
                "name": "workflow_failure",
                "description": "observable failure route",
                "steps": [
                    {
                        "id": "fail",
                        "capability": "execute",
                        "arguments": {"language": "shell", "code": "exit 7"},
                    }
                ],
            },
            "workflow-create-failure",
        )
        failed = await dispatch_after_approval(
            {"operation": "run", "workflow_id": failed_child.output},
            "workflow-run-failure",
        )
        assert failed.status is CapabilityResultStatus.FAILED
        failed_payload = json.loads(failed.output)
        assert failed_payload["status"] == "failed"
        assert failed_payload["failures"]
    finally:
        await service.stop()
