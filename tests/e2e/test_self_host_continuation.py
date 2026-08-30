"""End-to-end self-host mission continuity contracts."""

from __future__ import annotations

import json
from types import SimpleNamespace

from athena.service.service import AthenaService
from athena.self_host.controller import SelfHostMissionController


class _Planner:
    def __init__(self) -> None:
        self.calls = 0

    async def utility_inference(self, **_kwargs: object) -> str:
        self.calls += 1
        return json.dumps(
            {
                "done": False,
                "work_item": {
                    "title": f"bounded item {self.calls}",
                    "objective": f"execute bounded item {self.calls}",
                    "affected_files": ["src/athena/service/service.py"],
                    "affected_invariants": ["candidate-isolated"],
                    "dependencies": [],
                },
                "reason": "continue the mission with one bounded item",
            }
        )


class _Authority:
    source_revision = "source"
    design_bundle_hash = "design"
    gate_bundle_hash = "gates"

    def retrieve_design_context(self, **_kwargs: object) -> str:
        return "trusted frozen design context"

    def to_record(self) -> dict[str, str]:
        return {
            "source_revision": self.source_revision,
            "design_bundle_hash": self.design_bundle_hash,
            "gate_bundle_hash": self.gate_bundle_hash,
        }


async def test_self_host_plans_a_new_item_after_each_promotion():
    service = AthenaService.in_memory()
    planner = _Planner()
    service._kernel = planner  # noqa: SLF001 - exercise the service orchestration seam
    index = SimpleNamespace(
        index_revision="index-1",
        source_revision="source",
        files=({"path": "src/athena/service/service.py"},),
    )
    authority = _Authority()
    plan = SelfHostMissionController.initial_plan(
        "finish Athena",
        index=index,
        design_bundle_hash="design",
        gate_bundle_hash="gates",
        base_fingerprint="base",
    )

    plan, error = await service._plan_next_self_host_item(  # noqa: SLF001
        {"objective": "finish Athena", "plan": plan},
        plan=plan,
        current_index=index,
        bundle=authority,
        task_id=None,
        initial=True,
    )
    assert error is None
    first = plan["current_work_item"]
    plan = SelfHostMissionController.mark_promoted(
        SelfHostMissionController.mark_task(plan, "task-1"),
        branch_id="branch-1",
        certificate_hash="certificate-1",
        candidate_fingerprint="candidate-1",
    )

    plan, error = await service._plan_next_self_host_item(  # noqa: SLF001
        {"objective": "finish Athena", "plan": plan},
        plan=plan,
        current_index=index,
        bundle=authority,
        task_id="task-1",
    )

    assert error is None
    assert planner.calls == 2
    assert plan["current_work_item"]["objective"] != first["objective"]
    assert plan["completed_work_items"][0]["task_id"] == "task-1"
    assert plan["phase"] == "PLAN"
